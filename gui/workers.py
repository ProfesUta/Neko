"""One conversation owner, with user priority and guarded screen advice."""

import logging
import time
from threading import Event, Lock

from PySide6.QtCore import QThread, Signal

from config import ADVICE_MAX_AGE_SECONDS
from core.cancellation import AnyEvent, OperationCancelled, check_cancelled


class ConversationWorker(QThread):
    status_changed = Signal(str)
    user_text = Signal(str)
    response_ready = Signal(str)
    tool_result = Signal(str, str)
    inference_active = Signal(bool)
    error = Signal(str)
    interrupted = Signal()
    advice_ready = Signal(object)
    advice_finished = Signal(object)
    advice_error = Signal(str)

    def __init__(self, messages, context):
        super().__init__()
        self.messages = messages
        self.context = context
        self._stop_event = Event()
        self._spatial_generation = None

    def stop(self):
        self._stop_event.set()
        self.requestInterruption()

    def _answer(self, text):
        from core.chat import _trim_history, chat
        from core.spatial import spatial_request

        self.context.interaction.begin_user_turn()
        self.messages.append({"role": "user", "content": text})
        self.user_text.emit(text)
        check_cancelled(self._stop_event)
        if self.context.spatial is not None:
            self._spatial_generation = self.context.spatial.begin_turn(text, self._stop_event)
        if self.context.get_screen_status()["enabled"] and not (
            self.context.spatial is not None and spatial_request(text)
        ):
            self.status_changed.emit("Looking at screen")
            direct_answer = self.context.refresh_screen(text, self._stop_event, conversation=self.messages)
            check_cancelled(self._stop_event)
            if direct_answer:
                _trim_history(self.messages)
                self.messages.append({"role": "assistant", "content": direct_answer})
                return direct_answer
        check_cancelled(self._stop_event)
        self.status_changed.emit("Thinking")
        return chat(
            self.messages, self.context, speak_response=False,
            cancel_event=self._stop_event,
            status_callback=self.status_changed.emit,
            tool_callback=self.tool_result.emit,
        )

    def _prepare_spatial_response(self, answer):
        if self.context.spatial is not None:
            replacement = self.context.spatial.prepare_response(self._stop_event)
            answer = replacement or self.context.spatial.guard_response(answer)
            if self.messages and self.messages[-1].get("role") == "assistant":
                self.messages[-1]["content"] = answer
        return answer

    def _finish_spatial(self):
        if self.context.spatial is not None and self._spatial_generation is not None:
            self.context.spatial.finish_turn(self._spatial_generation)

    def _speak(self, answer, cancel_event=None, start_deadline=None):
        from voice.tts import speak

        check_cancelled(self._stop_event)
        self.status_changed.emit("Speaking")
        spoken_answer = answer
        options = {"start_deadline": start_deadline} if start_deadline is not None else {}
        if self.context.spatial is not None and self._spatial_generation is not None:
            def confirm_pointing():
                nonlocal spoken_answer
                replacement = self.context.spatial.prepare_response(self._stop_event)
                if replacement and replacement != spoken_answer:
                    spoken_answer = replacement
                    if self.messages and self.messages[-1].get("role") == "assistant":
                        self.messages[-1]["content"] = replacement
                    self.response_ready.emit(replacement)
                return replacement
            options["before_playback"] = confirm_pointing
        playback_started = False
        def started():
            nonlocal playback_started
            playback_started = True
            if self.context.spatial is not None and self._spatial_generation is not None:
                self.context.spatial.playback_started(self._spatial_generation)
        try:
            speak(answer, cancel_event=cancel_event or self._stop_event, on_playback_start=started, **options)
        finally:
            if playback_started:
                self.context.interaction.record_spoken(spoken_answer)

    def _play_advice(self, advice, cancel_event):
        try:
            if (
                not advice.is_fresh() or not self.context.is_screen_source_current(advice.source)
                or not self.context.interaction.allows_advice(advice.captured_at)
                or self.context.interaction.repeats_recent_response(advice.text)
            ):
                return
            cancellation = AnyEvent(self._stop_event, cancel_event)
            check_cancelled(cancellation)
            self.advice_ready.emit(advice)
            self._speak(
                advice.text, cancel_event=cancellation,
                start_deadline=advice.captured_at + ADVICE_MAX_AGE_SECONDS,
            )
        except OperationCancelled:
            check_cancelled(self._stop_event)
        except Exception as error:
            logging.exception("Screen tip playback failed")
            self.advice_error.emit(str(error))
        finally:
            self.advice_finished.emit(advice)

    def _report_error(self, error):
        logging.exception("Conversation failed")
        if isinstance(error, ModuleNotFoundError) and error.name in (
            "numpy", "sounddevice", "torch", "faster_whisper", "silero_vad", "piper",
        ):
            self.error.emit("Voice support is not installed. Run: python -m pip install -r requirements.txt")
        else:
            self.error.emit(str(error))


class ChatWorker(ConversationWorker):
    def __init__(self, messages, context, text, speak_reply=False):
        super().__init__(messages, context)
        self.text = text
        self.speak_reply = speak_reply

    def run(self):
        try:
            self.inference_active.emit(True)
            answer = self._prepare_spatial_response(self._answer(self.text))
            self.inference_active.emit(False)
            check_cancelled(self._stop_event)
            self.response_ready.emit(answer)
            speak_allowed = self.context.interaction.accept_answer(
                answer, self.text, suppress_duplicates=self.speak_reply,
            )
            if self.speak_reply and speak_allowed:
                self._speak(answer)
        except OperationCancelled:
            self.interrupted.emit()
        except Exception as error:
            self._report_error(error)
        finally:
            self._finish_spatial()
            self.context.interaction.finish_user_turn()
            self.inference_active.emit(False)


class AdviceWorker(ConversationWorker):
    def __init__(self, messages, context, advice):
        super().__init__(messages, context)
        self.advice = advice

    def run(self):
        try:
            self._play_advice(self.advice, self._stop_event)
        except OperationCancelled:
            pass


class VoiceWorker(ConversationWorker):
    def __init__(self, messages, context):
        super().__init__(messages, context)
        self._advice_lock = Lock()
        self._advice_available = Event()
        self._pending_advice = None
        self._active_advice_cancel = None
        self._speech_started_at = None

    def offer_advice(self, advice):
        if (
            not self.context.interaction.allows_advice(advice.captured_at)
            or self.context.interaction.repeats_recent_response(advice.text)
        ):
            return False
        with self._advice_lock:
            if self._pending_advice is not None or self._stop_event.is_set():
                return False
            self._pending_advice = (advice, Event())
            self._advice_available.set()
            return True

    def cancel_advice(self):
        with self._advice_lock:
            if self._pending_advice is not None:
                advice, token = self._pending_advice
                token.set()
                self._pending_advice = None
                self.advice_finished.emit(advice)
            if self._active_advice_cancel is not None:
                self._active_advice_cancel.set()
            self._advice_available.clear()

    def _deliver_pending_advice(self):
        with self._advice_lock:
            item = self._pending_advice
            self._pending_advice = None
            self._advice_available.clear()
            if item:
                self._active_advice_cancel = item[1]
        if item:
            try:
                self._play_advice(*item)
            finally:
                with self._advice_lock:
                    self._active_advice_cancel = None

    def _on_speech_start(self):
        # Set priority synchronously, before the GUI receives the status signal.
        self._speech_started_at = time.monotonic()
        self.context.interaction.begin_user_turn()
        self.cancel_advice()
        if self.context.spatial is not None:
            self.context.spatial.cancel()
        self.status_changed.emit("Speech detected")

    def run(self):
        try:
            self.inference_active.emit(True)
            self.status_changed.emit("Loading voice models")
            from voice.voice import load_models, record_until_silence, transcribe_audio
            load_models(self._stop_event)
            self.inference_active.emit(False)

            while not self._stop_event.is_set():
                self._deliver_pending_advice()
                check_cancelled(self._stop_event)
                if self._stop_event.wait(self.context.interaction.microphone_delay()):
                    break
                self.status_changed.emit("Listening")
                self._speech_started_at = None
                audio = record_until_silence(
                    self._stop_event,
                    on_speech_start=self._on_speech_start,
                    idle_interrupt_event=self._advice_available,
                )
                check_cancelled(self._stop_event)
                if audio is None:
                    continue

                try:
                    self.inference_active.emit(True)
                    try:
                        self.status_changed.emit("Transcribing")
                        text = transcribe_audio(audio, cancel_event=self._stop_event)
                        check_cancelled(self._stop_event)
                        if not text:
                            continue
                        if self.context.interaction.is_probable_echo(text, captured_at=self._speech_started_at):
                            logging.info("Ignored probable speaker echo; no user turn created")
                            continue
                        answer = self._prepare_spatial_response(self._answer(text))
                    finally:
                        self.inference_active.emit(False)
                    check_cancelled(self._stop_event)
                    if not self.context.interaction.accept_answer(answer, text, suppress_duplicates=True):
                        logging.info("Suppressed a repeated voice answer")
                        continue
                    self.response_ready.emit(answer)
                    try:
                        # A real user turn owns playback. Tips cannot cut it off.
                        self._speak(answer)
                    except OperationCancelled:
                        check_cancelled(self._stop_event)
                    except Exception as error:
                        self._report_error(error)
                finally:
                    self._finish_spatial()
                    self.context.interaction.finish_user_turn()
        except OperationCancelled:
            self.interrupted.emit()
        except Exception as error:
            self._report_error(error)
        finally:
            self.cancel_advice()
            self.context.interaction.finish_user_turn()
            self.inference_active.emit(False)

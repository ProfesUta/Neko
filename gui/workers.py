"""One conversation owner, with user priority and guarded screen advice."""

import logging
import json
import re
import time
from threading import Event, Lock

from PySide6.QtCore import QThread, Signal

from config import ADVICE_MAX_AGE_SECONDS
import config
from core.cancellation import AnyEvent, OperationCancelled, check_cancelled
from core.companion_style import reply_delivery


class _PreparedReply:
    def __init__(self, speech, on_finished):
        self.speech = speech
        self.on_finished = on_finished

    def play(self):
        try:
            self.speech.play()
        finally:
            self.on_finished()

    def close(self):
        self.speech.close()


class VoiceWarmupWorker(QThread):
    """Load recognition/VAD and speech output; never record or play audio."""
    status_changed = Signal(str)
    warning = Signal(str)

    def __init__(self):
        super().__init__()
        self._stop_event = Event()

    def stop(self):
        self._stop_event.set()
        self.requestInterruption()

    def run(self):
        try:
            from voice.voice import load_models
            from voice.tts import warm_up
            errors = []
            for label, prepare in (("recognition", load_models), ("speech output", warm_up)):
                check_cancelled(self._stop_event)
                self.status_changed.emit(f"Preparing {label}")
                try:
                    prepare(self._stop_event)
                except OperationCancelled:
                    raise
                except Exception as error:
                    errors.append(f"{label}: {error}")
            check_cancelled(self._stop_event)
            if errors:
                self.warning.emit("Voice preload incomplete (can retry when enabled): " + "; ".join(errors))
            else:
                self.status_changed.emit("Voice models ready" if config.TTS_PERSISTENT_ENABLED else
                                         "Recognition ready; speech output loads when used")
        except OperationCancelled:
            pass
        except Exception as error:
            self.warning.emit(f"Voice preload unavailable: {error}")


class LeagueWorker(QThread):
    """One owned asyncio loop for all League I/O. Stop cancels open requests."""
    notification = Signal(object)
    warning = Signal(str)

    def __init__(self, league):
        super().__init__()
        self.league = league
        self._stop_event = Event()
        self._loop = self._task = None

    def stop(self):
        self._stop_event.set()
        self.requestInterruption()
        try:
            if self._loop and self._task:
                self._loop.call_soon_threadsafe(self._task.cancel)
        except RuntimeError:
            pass  # The event loop already finished.

    def run(self):
        import asyncio
        async def serve():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            if not self._stop_event.is_set():
                await self.league.run()
        self.league.on_notification = self.notification.emit
        try:
            asyncio.run(serve())
        except asyncio.CancelledError:
            pass
        except Exception:
            # Do not stringify network errors, which can contain auth headers.
            logging.warning("League awareness stopped; check League configuration and restart Capri.")
            self.warning.emit("League awareness unavailable. Check League configuration.")
        finally:
            self.league.on_notification = None
            self._task = self._loop = None
            if self.league.metrics.enabled:
                logging.info("League latency summary (milliseconds): %s", json.dumps(self.league.metrics.report()))


class ConversationWorker(QThread):
    status_changed = Signal(str)
    user_text = Signal(str)
    response_ready = Signal(str)
    response_partial = Signal(str)
    reaction_ready = Signal(str)
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
        self._spatial_response_prepared = False
        self._streamed_reply = False
        self._speak_reply_enabled = False
        self._turn_started = None
        self._first_audio_logged = False
        self._question = ""

    def stop(self):
        self._stop_event.set()
        self.requestInterruption()

    def _answer(self, text):
        from core.chat import _trim_history, chat
        from core.spatial import spatial_request
        from core.quick_actions import parse_location_request, location_reply
        from core.conversation import needs_fresh_screen, needs_tools, converse
        from core.league import league_question
        from core.response_grounding import ambiguous_question, is_proactive, information_request
        from core.screen_access import screen_capability_question, screen_status_reply

        started = time.monotonic()
        self._question = text
        self._turn_started = started
        self._first_audio_logged = False
        self._spatial_response_prepared = False
        self._streamed_reply = False
        self.context.interaction.begin_user_turn()
        self.context.pause_background_screen()
        self.messages.append({"role": "user", "content": text})
        self.user_text.emit(text)
        check_cancelled(self._stop_event)
        recent_users = [item for item in self.messages[:-1] if item.get("role") == "user" and not is_proactive(item)][-4:]
        league_request = league_question(text, recent_users, self.context.league.champ_select.names.names.values())
        if league_request and information_request(text) and not needs_tools(text):
            direct_answer = self._league_reply(text)
            _trim_history(self.messages)
            self.messages.append({"role": "assistant", "content": direct_answer})
            return direct_answer
        clarification = ambiguous_question(text, self.messages)
        if clarification:
            _trim_history(self.messages)
            self.messages.append({"role": "assistant", "content": clarification})
            return clarification
        if self.context.spatial is not None:
            self._spatial_generation = self.context.spatial.begin_turn(text, self._stop_event)
            quick = parse_location_request(text) if config.SPATIAL_QUICK_ACTIONS_ENABLED else None
            if quick is not None:
                self.status_changed.emit("Locating on screen")
                if self.context.spatial.enabled():
                    result = self.context.spatial.show_target(quick.query, quick.monitor_id, self._stop_event)
                else:
                    result = {"status": "disabled", "message": "Turn VISION ON so I can locate things on your screen, chum."}
                check_cancelled(self._stop_event)
                self._spatial_response_prepared = result.get("status") == "pointing"
                self.tool_result.emit("show_target", json.dumps(result, ensure_ascii=False))
                answer = location_reply(result, quick, self._spatial_generation)
                _trim_history(self.messages)
                self.messages.append({"role": "assistant", "content": answer})
                logging.info("Direct spatial action: %s in %.0f ms (no chat model)",
                             result.get("status"), (time.monotonic() - started) * 1000)
                return answer
        game = None
        screen_status = self.context.get_screen_status()
        screen_enabled = screen_status["enabled"]
        capability = screen_capability_question(text)
        if capability and (not screen_enabled or screen_status["available"] or screen_status.get("live", {}).get("capturing")):
            answer = screen_status_reply(self.context)
            _trim_history(self.messages)
            self.messages.append({"role": "assistant", "content": answer})
            return answer
        if screen_enabled and (capability or needs_fresh_screen(text) or not config.CONVERSATION_STREAMING_ENABLED) and not (
            self.context.spatial is not None and spatial_request(text)
            and not re.search(r"\bwhere.{0,25}(enemy|jungler)\b", text, re.I)
        ):
            self.status_changed.emit("Looking at screen")
            direct_answer = self.context.refresh_screen(text, self._stop_event, conversation=self.messages)
            check_cancelled(self._stop_event)
            status = self.context.get_screen_status()
            if capability or (not needs_tools(text) and (status["error"] or not status["enabled"])):
                direct_answer = screen_status_reply(self.context)
            if direct_answer:
                _trim_history(self.messages)
                self.messages.append({"role": "assistant", "content": direct_answer})
                return direct_answer
        check_cancelled(self._stop_event)
        self.status_changed.emit("Thinking")
        if (config.CONVERSATION_STREAMING_ENABLED and not needs_tools(text)
                and not (spatial_request(text) and needs_fresh_screen(text))):
            from core.streaming import StreamingReply
            # The callback runs only after feed; a model failure also stops audio.
            speech = None
            options = {}
            if self._speak_reply_enabled:
                if config.TTS_PREFETCH_ENABLED:
                    speech = lambda prepared: prepared.play()
                    options = {
                        "prepare": lambda sentence: self._prepare_stream_sentence(sentence, stream.cancellation),
                        "discard": lambda prepared: prepared.close(),
                        "min_segment_chars": config.TTS_STREAM_MIN_CHARS,
                    }
                else:
                    speech = lambda sentence: self._stream_sentence(sentence, stream.cancellation)
            stream = StreamingReply(self._stop_event, self.context.interaction, text,
                                    self.response_partial.emit, speak=speech, **options)
            try:
                _trim_history(self.messages)
                answer = converse(self.messages, self.context, stream.cancellation, stream.feed, game)
                if not stream.text:
                    stream.feed(answer)
                stream.finish()
                self._streamed_reply = True
                return answer
            except BaseException as error:
                stream.abort()
                if stream.text and self.messages[-1].get("role") == "user":
                    self.messages.append({"role": "assistant", "content": stream.text + " [Interrupted]"})
                if isinstance(error, OperationCancelled) and stream.error and not self._stop_event.is_set():
                    raise stream.error from error
                raise
        return chat(
            self.messages, self.context, speak_response=False,
            cancel_event=self._stop_event,
            status_callback=self.status_changed.emit,
            tool_callback=self.tool_result.emit,
        )

    def _league_reply(self, text):
        """API cache -> current screen -> grounded reply, with no chat-model detour."""
        from core.response_grounding import UNVERIFIED
        from core.screen_access import screen_status_reply
        metrics = self.context.league.metrics
        started = metrics.start()
        try:
            self.status_changed.emit("Checking League API")
            answer = self.context.league.answer(text, verified_only=True)
            evidence = self.context.league.context_for(text) or {"own_player": None}
            evidence = {**evidence, "api_checked": True}
            check_cancelled(self._stop_event)
            self.status_changed.emit("Checking screen")
            if answer:
                checked = self.context.check_screen(self._stop_event)
                check_cancelled(self._stop_event)
                if checked["status"] != "checked":
                    return "The League API says: " + answer + " I couldn't also check the screen right now."
                return answer
            status = self.context.get_screen_status()
            if not status["enabled"]:
                return "I checked the League API, but it doesn't verify that answer. Turn VISION ON so I can check the screen too."
            answer = self.context.refresh_screen(text, self._stop_event, conversation=self.messages, league_context=evidence)
            check_cancelled(self._stop_event)
            if self.context.get_screen_status()["error"]:
                return "I checked the League API. " + screen_status_reply(self.context)
            return answer or UNVERIFIED
        finally:
            metrics.finish("league_api_screen_reply", started)

    def _prepare_spatial_response(self, answer):
        if self.context.spatial is not None and self._spatial_generation is not None:
            # A direct result was just acknowledged by the GUI with no intervening
            # inference. Speech still revalidates after synthesis, before playback.
            replacement = None if self._spatial_response_prepared else self.context.spatial.prepare_response(self._stop_event)
            self._spatial_response_prepared = False
            answer = replacement or self.context.spatial.guard_response(answer)
            if self.messages and self.messages[-1].get("role") == "assistant":
                self.messages[-1]["content"] = answer
        return answer

    def _finish_spatial(self):
        generation, self._spatial_generation = self._spatial_generation, None
        self._turn_started = None
        self._question = ""
        if self.context.spatial is not None and generation is not None:
            self.context.spatial.finish_turn(generation)

    def _stream_sentence(self, sentence, cancellation):
        if self.context.spatial is not None:
            sentence = self.context.spatial.guard_response(sentence)
        self._speak(sentence, cancel_event=cancellation)

    def _prepare_stream_sentence(self, sentence, cancellation):
        if self.context.spatial is not None:
            sentence = self.context.spatial.guard_response(sentence)
        return self._speak(sentence, cancel_event=cancellation, prepare_only=True)

    def _speak(self, answer, cancel_event=None, start_deadline=None, urgent=False, prepare_only=False,
               playback_guard=None, on_started=None):
        from voice.tts import prepare_speech, speak

        check_cancelled(self._stop_event)
        if not prepare_only:
            self.status_changed.emit("Speaking")
        spoken_answer = answer
        delivery = reply_delivery(self._question, answer, urgent=urgent)
        options = {"start_deadline": start_deadline} if start_deadline is not None else {}
        options["style"] = delivery.style
        if playback_guard is not None:
            def guard():
                if not playback_guard():
                    raise OperationCancelled()
            options["before_playback"] = guard
        if self.context.spatial is not None and self._spatial_generation is not None:
            def confirm_pointing():
                nonlocal spoken_answer, delivery
                replacement = self.context.spatial.prepare_response(self._stop_event)
                if replacement and replacement != spoken_answer:
                    spoken_answer = replacement
                    delivery = reply_delivery(self._question, replacement, urgent=urgent)
                    if self.messages and self.messages[-1].get("role") == "assistant":
                        self.messages[-1]["content"] = replacement
                    self.response_ready.emit(replacement)
                return replacement
            options["before_playback"] = confirm_pointing
        playback_started = False
        def started():
            nonlocal playback_started
            playback_started = True
            if on_started:
                on_started()
            self.status_changed.emit("Speaking")
            if self._turn_started is not None and not self._first_audio_logged:
                logging.info("First speech audio: %.0f ms after transcript", (time.monotonic() - self._turn_started) * 1000)
                self._first_audio_logged = True
            if self.context.spatial is not None and self._spatial_generation is not None:
                self.context.spatial.playback_started(self._spatial_generation)
            if delivery.expression and config.COMPANION_REACTIONS_ENABLED:
                self.reaction_ready.emit(delivery.expression)
        def finished():
            if playback_started:
                self.context.interaction.record_spoken(spoken_answer)
        if prepare_only:
            prepared = prepare_speech(answer, cancel_event=cancel_event or self._stop_event,
                                      on_playback_start=started, **options)
            return _PreparedReply(prepared, finished) if prepared is not None else None
        try:
            speak(answer, cancel_event=cancel_event or self._stop_event, on_playback_start=started, **options)
        finally:
            finished()

    def _play_advice(self, advice, cancel_event):
        from core.league_voice import LeagueAnnouncement
        league_event = isinstance(advice, LeagueAnnouncement)
        def current():
            return (advice.is_fresh() and self.context.interaction.allows_advice(advice.captured_at)
                    and not self.context.interaction.repeats_recent_response(advice.text)
                    and (self.context.league.announcement_current(advice) and config.LEAGUE_PROACTIVE_VOICE_ENABLED
                         if league_event else self.context.is_screen_source_current(advice.source)))
        try:
            if not current():
                return
            cancellation = AnyEvent(self._stop_event, cancel_event)
            check_cancelled(cancellation)
            self.advice_ready.emit(advice)
            options = {"playback_guard": current}
            if league_event:
                options["on_started"] = lambda: self.context.league.metrics.finish("notification_to_audio", advice.queued_ns)
            self._speak(advice.text, cancel_event=cancellation,
                        start_deadline=advice.captured_at + (advice.lifetime if league_event else ADVICE_MAX_AGE_SECONDS),
                        urgent=not league_event, **options)
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
            "numpy", "sounddevice", "torch", "faster_whisper", "silero_vad", "sherpa_onnx",
        ):
            self.error.emit("Voice support is not installed. Run: python -m pip install -r requirements.txt")
        else:
            self.error.emit(str(error))


class ChatWorker(ConversationWorker):
    def __init__(self, messages, context, text, speak_reply=False):
        super().__init__(messages, context)
        self.text = text
        self.speak_reply = speak_reply
        self._speak_reply_enabled = speak_reply

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
            if self.speak_reply and speak_allowed and not self._streamed_reply:
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
        self._speak_reply_enabled = True
        self._advice_lock = Lock()
        self._advice_available = Event()
        self._pending_advice = None
        self._active_advice_cancel = None
        self._speech_started_at = None
        self._microphone_muted = Event()
        self.context.interaction.set_microphone_enabled(True)

    def microphone_enabled(self):
        return not self._microphone_muted.is_set() and not self._stop_event.is_set()

    def mute_microphone(self):
        self._microphone_muted.set()
        self.context.interaction.set_microphone_enabled(False)

    def offer_advice(self, advice):
        if (
            not self.context.interaction.allows_advice(advice.captured_at)
            or self.context.interaction.repeats_recent_response(advice.text)
        ):
            return False
        with self._advice_lock:
            if self._pending_advice is not None or not self.microphone_enabled():
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
        if not self.microphone_enabled():
            return
        # Set priority synchronously, before the GUI receives the status signal.
        self._speech_started_at = time.monotonic()
        self.context.interaction.begin_user_turn()
        self.context.pause_background_screen()
        self.inference_active.emit(True)
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
            if config.TTS_PERSISTENT_ENABLED:
                from voice.tts import warm_up
                try:
                    self.status_changed.emit("Loading speech output")
                    warm_up(self._stop_event)
                except OperationCancelled:
                    raise
                except Exception as error:
                    logging.warning("Speech output warm-up unavailable: %s", error)
            self.inference_active.emit(False)

            recording_cancellation = AnyEvent(self._stop_event, self._microphone_muted)
            while self.microphone_enabled():
                self._deliver_pending_advice()
                check_cancelled(self._stop_event)
                if recording_cancellation.wait(self.context.interaction.microphone_delay()):
                    break
                self.status_changed.emit("Listening")
                self._speech_started_at = None
                audio = record_until_silence(
                    recording_cancellation,
                    on_speech_start=self._on_speech_start,
                    idle_interrupt_event=self._advice_available,
                )
                check_cancelled(self._stop_event)
                if self._microphone_muted.is_set():
                    break
                if audio is None:
                    self.context.interaction.finish_user_turn()
                    self.inference_active.emit(False)
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
                    speak_allowed = self.context.interaction.accept_answer(answer, text, suppress_duplicates=True)
                    if not speak_allowed and not self._streamed_reply:
                        logging.info("Suppressed a repeated voice answer")
                        continue
                    self.response_ready.emit(answer)
                    try:
                        # A real user turn owns playback. Tips cannot cut it off.
                        if speak_allowed and not self._streamed_reply:
                            self._speak(answer)
                    except OperationCancelled:
                        check_cancelled(self._stop_event)
                    except Exception as error:
                        self._report_error(error)
                finally:
                    self._finish_spatial()
                    self.context.interaction.finish_user_turn()
        except OperationCancelled:
            if self._stop_event.is_set():
                self.interrupted.emit()
        except Exception as error:
            self._report_error(error)
        finally:
            self.cancel_advice()
            self.context.interaction.set_microphone_enabled(False)
            self.context.interaction.finish_user_turn()
            self.inference_active.emit(False)

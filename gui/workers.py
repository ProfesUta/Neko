"""One conversation owner, with user priority and guarded screen advice."""

import logging
import json
import re
import time
from threading import Event, Lock, RLock

from PySide6.QtCore import QThread, Signal

from config import ADVICE_MAX_AGE_SECONDS
import config
from core.cancellation import AnyEvent, OperationCancelled, check_cancelled
from core.companion_style import reply_delivery
from core.inference_priority import priority as inference_priority
from core.latency import start_trace, clear_trace, use_trace, current_trace


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
            if config.CONVERSATION_PRELOAD_ON_STARTUP:
                from core.conversation import warm_up as warm_chat
                try:
                    self.status_changed.emit("Preparing conversation model")
                    warm_chat(self._stop_event)
                except OperationCancelled:
                    # Foreground work may preempt this optional model warm-up.
                    check_cancelled(self._stop_event)
                except Exception:
                    logging.warning("Conversation warm-up unavailable; the next request will retry")
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
    session_active_changed = Signal(bool)
    session_display_changed = Signal(object)
    visual_state_changed = Signal(object)

    def __init__(self, league, grounder=None):
        super().__init__()
        self.league = league
        self._stop_event = Event()
        self._loop = self._task = None
        self._grounder = grounder
        self._session_active = False
        self._game_window_found = False
        self._display_bounds = None
        self._display_scan_phase = None

    def _resolve_session_display(self):
        """Bounded native metadata scan on the existing League worker, never UI."""
        if self._grounder is None or self._stop_event.is_set():
            return
        self._display_scan_phase = self.league.champ_select.snapshot().phase
        try:
            windows = self._grounder.windows(self._stop_event)
            game = [window for window in windows if window.application.casefold() == "league of legends.exe"]
            window = next(iter(game), None)
            bounds = window.bounds if window else None
            self._game_window_found = bool(game)
            if bounds != self._display_bounds:
                self._display_bounds = bounds
                self.session_display_changed.emit(bounds)
        except OperationCancelled:
            pass
        except Exception:
            logging.warning("Could not identify the League display; using the primary display")

    def _session_changed(self, active):
        if active and not config.LEAGUE_COMPANION_MODE_ENABLED:
            return
        if active == self._session_active:
            return
        self._session_active = active
        if active:
            self._resolve_session_display()
        else:
            self._display_bounds = None
            self._game_window_found = False
            self._display_scan_phase = None
        self.session_active_changed.emit(active)

    def _gameflow_changed(self, phase):
        if (self._session_active and phase in ("GameStart", "InProgress", "Reconnect")
                and not self._game_window_found and phase != self._display_scan_phase):
            # The game window can appear after champion-select docking. This is
            # phase driven, not a second poller or a per-event window scan.
            self._resolve_session_display()

    def _live_game_ready(self):
        if self._session_active and not self._game_window_found:
            self._resolve_session_display()

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
        self.league.on_session_active_changed = self._session_changed
        self.league.on_gameflow_changed = self._gameflow_changed
        self.league.on_live_game_ready = self._live_game_ready
        self.league.on_visual_state_changed = self.visual_state_changed.emit
        self._session_changed(self.league.session_active)
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
            self._session_changed(False)
            self.league.on_session_active_changed = None
            self.league.on_gameflow_changed = None
            self.league.on_live_game_ready = None
            self.league.on_visual_state_changed = None
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
    advice_started = Signal(object)
    advice_error = Signal(str)
    playback_active_changed = Signal(bool)

    def __init__(self, messages, context):
        super().__init__()
        self.messages = messages
        self.context = context
        self._stop_event = Event()
        self._turn_cancel = Event()
        self._cancellation = AnyEvent(self._stop_event, self._turn_cancel)
        self._spatial_generation = None
        self._spatial_response_prepared = False
        self._streamed_reply = False
        self._speak_reply_enabled = False
        self._turn_started = None
        self._first_audio_logged = False
        self._question = ""
        self._trace = None
        self._playback_lock = RLock()
        self._playback_active = False

    def _set_playback_active(self, active):
        with self._playback_lock:
            if active and self._cancellation.is_set():
                return
            if active == self._playback_active:
                return
            self._playback_active = active
            # Preserve start/stop signal order when the audio callback and a
            # microphone interruption race. GUI connections remain queued.
            self.playback_active_changed.emit(active)

    def stop(self):
        self._mark_interruption()
        self._stop_event.set()
        self._set_playback_active(False)
        self.requestInterruption()

    def _mark_interruption(self):
        trace = self._trace
        if trace is not None:
            trace.mark("interruption_detected")
        incoming = current_trace()
        if incoming is not None and incoming is not trace:
            incoming.mark("interruption_detected")

    def _answer(self, text):
        from core.chat import _trim_history, chat
        from core.spatial import spatial_request
        from core.quick_actions import parse_location_request, location_reply
        from core.conversation import needs_fresh_screen, needs_tools, converse
        from core.league import league_question
        from core.response_grounding import ambiguous_question, is_proactive, information_request
        from core.screen_access import screen_capability_question, screen_status_reply

        started = time.monotonic()
        self._trace = current_trace()
        inference_priority.user_started(self)
        self._question = text
        self._turn_started = started
        self._first_audio_logged = False
        self._spatial_response_prepared = False
        self._streamed_reply = False
        self.context.interaction.begin_user_turn()
        self.context.pause_background_screen()
        self.messages.append({"role": "user", "content": text})
        self.user_text.emit(text)
        check_cancelled(self._cancellation)
        handler = getattr(self.context.league, "companion_answer", None)
        companion_answer = handler(text, self._cancellation) if handler else None
        if companion_answer:
            _trim_history(self.messages)
            self.messages.append({"role": "assistant", "content": companion_answer})
            return companion_answer
        postgame = getattr(self.context.league,"postgame_context",lambda text:None)(text)
        recent_users = [item for item in self.messages[:-1] if item.get("role") == "user" and not is_proactive(item)][-4:]
        league_request = not postgame and league_question(text, recent_users, self.context.league.champ_select.names.names.values())
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
            self._spatial_generation = self.context.spatial.begin_turn(text, self._cancellation)
            quick = parse_location_request(text) if config.SPATIAL_QUICK_ACTIONS_ENABLED else None
            if quick is not None:
                self.status_changed.emit("Locating on screen")
                if self.context.spatial.enabled():
                    result = self.context.spatial.show_target(quick.query, quick.monitor_id, self._cancellation)
                else:
                    result = {"status": "disabled", "message": "Turn VISION ON so I can locate things on your screen, chum."}
                check_cancelled(self._cancellation)
                self._spatial_response_prepared = result.get("status") == "pointing"
                self.tool_result.emit("show_target", json.dumps(result, ensure_ascii=False))
                answer = location_reply(result, quick, self._spatial_generation)
                _trim_history(self.messages)
                self.messages.append({"role": "assistant", "content": answer})
                logging.info("Direct spatial action: %s in %.0f ms (no chat model)",
                             result.get("status"), (time.monotonic() - started) * 1000)
                return answer
        game = {"post_game_review":postgame} if postgame else None
        screen_status = self.context.get_screen_status()
        screen_enabled = screen_status["enabled"] and not postgame
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
            direct_answer = self.context.refresh_screen(text, self._cancellation, conversation=self.messages)
            check_cancelled(self._cancellation)
            status = self.context.get_screen_status()
            if capability or (not needs_tools(text) and (status["error"] or not status["enabled"])):
                direct_answer = screen_status_reply(self.context)
            if direct_answer:
                _trim_history(self.messages)
                self.messages.append({"role": "assistant", "content": direct_answer})
                return direct_answer
        check_cancelled(self._cancellation)
        self.status_changed.emit("Thinking")
        if (config.CONVERSATION_STREAMING_ENABLED and (postgame or not needs_tools(text))
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
                        "first_chunk_max_chars": config.TTS_FIRST_CHUNK_MAX_CHARS,
                        "first_sentence_min_chars": config.TTS_FIRST_SENTENCE_MIN_CHARS,
                        "text_queue_limit": config.TTS_TEXT_QUEUE_LIMIT,
                    }
                    if config.TTS_NATIVE_STREAMING_ENABLED:
                        options["prepare_stream"] = lambda sentence, ready: self._prepare_stream_chunks(sentence, stream.cancellation, ready)
                else:
                    speech = lambda sentence: self._stream_sentence(sentence, stream.cancellation)
            stream = StreamingReply(self._cancellation, self.context.interaction, text,
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
                if isinstance(error, OperationCancelled) and stream.error and not self._cancellation.is_set():
                    raise stream.error from error
                raise
            finally:
                self._set_playback_active(False)
        return chat(
            self.messages, self.context, speak_response=False,
            cancel_event=self._cancellation,
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
            check_cancelled(self._cancellation)
            self.status_changed.emit("Checking screen")
            if answer:
                checked = self.context.check_screen(self._cancellation)
                check_cancelled(self._cancellation)
                if checked["status"] != "checked":
                    return "The League API says: " + answer + " I couldn't also check the screen right now."
                return answer
            status = self.context.get_screen_status()
            if not status["enabled"]:
                return "I checked the League API, but it doesn't verify that answer. Turn VISION ON so I can check the screen too."
            answer = self.context.refresh_screen(text, self._cancellation, conversation=self.messages, league_context=evidence)
            check_cancelled(self._cancellation)
            if self.context.get_screen_status()["error"]:
                return "I checked the League API. " + screen_status_reply(self.context)
            return answer or UNVERIFIED
        finally:
            metrics.finish("league_api_screen_reply", started)

    def _prepare_spatial_response(self, answer):
        if self.context.spatial is not None and self._spatial_generation is not None:
            # A direct result was just acknowledged by the GUI with no intervening
            # inference. Speech still revalidates after synthesis, before playback.
            replacement = None if self._spatial_response_prepared else self.context.spatial.prepare_response(self._cancellation)
            self._spatial_response_prepared = False
            answer = replacement or self.context.spatial.guard_response(answer)
            if self.messages and self.messages[-1].get("role") == "assistant":
                self.messages[-1]["content"] = answer
        return answer

    def _finish_spatial(self):
        trace, self._trace = self._trace, None
        if trace is not None:
            trace.mark("interrupted_turn_stopped")
        inference_priority.user_finished(self)
        clear_trace()
        generation, self._spatial_generation = self._spatial_generation, None
        self._turn_started = None
        self._question = ""
        if self.context.spatial is not None and generation is not None:
            self.context.spatial.finish_turn(generation)

    def _stream_sentence(self, sentence, cancellation):
        if self.context.spatial is not None:
            sentence = self.context.spatial.guard_response(sentence)
        self._speak(sentence, cancel_event=cancellation, stream_segment=True)

    def _prepare_stream_sentence(self, sentence, cancellation):
        if self.context.spatial is not None:
            sentence = self.context.spatial.guard_response(sentence)
        return self._speak(sentence, cancel_event=cancellation, prepare_only=True)

    def _prepare_stream_chunks(self, sentence, cancellation, ready):
        if self.context.spatial is not None:
            sentence = self.context.spatial.guard_response(sentence)
        return self._speak(sentence, cancel_event=cancellation, prepare_only=True, deliver=ready)

    def _speak(self, answer, cancel_event=None, start_deadline=None, urgent=False, prepare_only=False,
               playback_guard=None, on_started=None, deliver=None, stream_segment=False):
        from voice.tts import prepare_speech, speak, stream_speech

        check_cancelled(self._cancellation)
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
        if (self.context.spatial is not None and self._spatial_generation is not None
                and getattr(self.context.spatial, "response_requires_confirmation", lambda: True)()):
            def confirm_pointing():
                nonlocal spoken_answer, delivery
                replacement = self.context.spatial.prepare_response(self._cancellation)
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
            self._set_playback_active(True)
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
            if deliver is not None and not options.get("before_playback") and start_deadline is None:
                return stream_speech(answer, deliver, cancel_event=cancel_event or self._cancellation,
                                     on_playback_start=started, on_finished=finished, style=delivery.style)
            prepared = prepare_speech(answer, cancel_event=cancel_event or self._cancellation,
                                      on_playback_start=started, **options)
            result = _PreparedReply(prepared, finished) if prepared is not None else None
            if deliver is not None:
                if result is not None:
                    deliver(result)
                return
            return result
        try:
            speak(answer, cancel_event=cancel_event or self._cancellation, on_playback_start=started, **options)
        finally:
            finished()
            if not stream_segment:
                self._set_playback_active(False)

    def _play_advice(self, advice, cancel_event):
        from core.league_voice import LeagueAnnouncement
        league_event = isinstance(advice, LeagueAnnouncement)
        trace = start_trace()
        self._trace = trace
        if trace and league_event and advice.queued_ns:
            trace.mark("league_detected", advice.detected_ns or advice.queued_ns)
            if advice.first_raw_ns:trace.mark("visual_first_raw",advice.first_raw_ns)
            if advice.confirmed_ns:trace.mark("visual_confirmed",advice.confirmed_ns)
            if advice.event_key.startswith("visual:"):trace.mark("visual_proposed",advice.queued_ns)
            if advice.accepted_ns:trace.mark("visual_accepted",advice.accepted_ns)
        def current():
            return (advice.is_fresh() and self.context.interaction.allows_advice(advice.captured_at)
                    and not self.context.interaction.repeats_recent_response(advice.text)
                    and (self.context.league.announcement_current(advice) and config.LEAGUE_PROACTIVE_VOICE_ENABLED
                         if league_event else self.context.is_screen_source_current(advice.source)))
        try:
            if not current():
                return
            events=[self._cancellation,cancel_event]
            if league_event and advice.cancel_event is not None:events.append(advice.cancel_event)
            cancellation = AnyEvent(*events)
            check_cancelled(cancellation)
            visual_event=league_event and advice.event_key.startswith("visual:")
            if not visual_event:self.advice_ready.emit(advice)
            options = {"playback_guard": current}
            if league_event:
                def started():
                    self.context.league.announcement_status(advice,"started")
                    self.context.league.metrics.finish("notification_to_audio", advice.queued_ns)
                    self.advice_started.emit(advice)
                    if visual_event:self.advice_ready.emit(advice)
                options["on_started"] = started
            self._speak(advice.text, cancel_event=cancellation,
                        start_deadline=advice.captured_at + (advice.lifetime if league_event else ADVICE_MAX_AGE_SECONDS),
                        urgent=not league_event, **options)
        except OperationCancelled:
            check_cancelled(self._stop_event)
        except Exception as error:
            logging.exception("Screen tip playback failed")
            self.advice_error.emit(str(error))
        finally:
            if league_event:self.context.league.announcement_status(advice,"dropped")
            self.advice_finished.emit(advice)
            if trace is not None:
                trace.mark("interrupted_turn_stopped")
            self._trace = None
            clear_trace()

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
        if current_trace() is None:
            start_trace()
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
        finally:
            self._set_playback_active(False)


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
        self._turn_lock = Lock()
        self._input_generation = 0
        self._turn_active = False
        self._input_pump = None
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
                self._mark_interruption()
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
        inference_priority.user_started(self)
        self.context.interaction.begin_user_turn()
        self.context.pause_background_screen()
        self.inference_active.emit(True)
        self.cancel_advice()
        if self.context.spatial is not None:
            self.context.spatial.cancel()
        self.status_changed.emit("Speech detected")

    def _barge_in_started(self):
        with self._turn_lock:
            if not self.microphone_enabled():
                return self._input_generation
            self._input_generation += 1
            if self._turn_active:
                self._mark_interruption()
                self._turn_cancel.set()
            generation = self._input_generation
        self._on_speech_start()
        self._set_playback_active(False)
        return generation

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
            if config.VOICE_BARGE_IN_ENABLED:
                from voice.capture import CapturePump
                self._input_pump = CapturePump(recording_cancellation, self._barge_in_started, record_until_silence)
                self._input_pump.start()
            while self.microphone_enabled():
                self._deliver_pending_advice()
                check_cancelled(self._stop_event)
                # Headphone capture is already live; a speaker echo-tail delay
                # would unnecessarily hold a newly captured interruption.
                delay = self.context.interaction.microphone_delay() if self._input_pump is None else 0
                if recording_cancellation.wait(delay):
                    break
                self.status_changed.emit("Listening")
                if self._input_pump is not None:
                    item = self._input_pump.take(recording_cancellation, self._advice_available)
                    audio = item.audio if item is not None else None
                    if item is not None:
                        with self._turn_lock:
                            if item.generation != self._input_generation:
                                continue
                            self._turn_cancel.clear()
                            self._turn_active = True
                        self._speech_started_at = item.started_at
                        use_trace(item.trace)
                else:
                    start_trace()
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
                    if self._input_pump is None or not self._input_pump.has_input():
                        inference_priority.user_finished(self)
                        clear_trace()
                        self.context.interaction.finish_user_turn()
                    self.inference_active.emit(False)
                    continue

                try:
                    self.inference_active.emit(True)
                    try:
                        self.status_changed.emit("Transcribing")
                        text = transcribe_audio(audio, cancel_event=self._cancellation)
                        check_cancelled(self._cancellation)
                        if not text:
                            continue
                        if self.context.interaction.is_probable_echo(text, captured_at=self._speech_started_at):
                            logging.info("Ignored probable speaker echo; no user turn created")
                            continue
                        answer = self._prepare_spatial_response(self._answer(text))
                    finally:
                        self.inference_active.emit(False)
                    check_cancelled(self._cancellation)
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
                except OperationCancelled:
                    check_cancelled(self._stop_event)
                finally:
                    with self._turn_lock:
                        self._turn_active = False
                    self._finish_spatial()
                    self.context.interaction.finish_user_turn()
                    if self._input_pump is not None and self._input_pump.has_input():
                        inference_priority.user_started(self)
                        self.context.interaction.begin_user_turn()
        except OperationCancelled:
            if self._stop_event.is_set():
                self.interrupted.emit()
        except Exception as error:
            self._report_error(error)
        finally:
            if self._input_pump is not None:
                try:
                    self._input_pump.close()
                except Exception as error:
                    self._report_error(error)
            self._set_playback_active(False)
            inference_priority.user_finished(self)
            clear_trace()
            self.cancel_advice()
            self.context.interaction.set_microphone_enabled(False)
            self.context.interaction.finish_user_turn()
            self.inference_active.emit(False)

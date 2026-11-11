import asyncio
import json
import sys
import time
from threading import Event, Thread, get_ident
from types import SimpleNamespace

import httpx
import numpy as np
import ollama
import pytest

import config
from core import latency, model_client
from core.cancellation import OperationCancelled, check_cancelled
from core.inference_priority import InferencePriority
from core.interaction import InteractionState
from core.league_metrics import LeagueMetrics
from core.streaming import StreamingReply
from voice import tts
from voice.playback import PlaybackSession


def test_metrics_measure_stage_durations_once_across_threads_without_payloads():
    metrics = LeagueMetrics(True)
    trace = latency.LatencyTrace(metrics)
    trace.mark("speech_end_estimate", 1_000_000)
    trace.mark("vad_detected", 501_000_000)
    trace.mark("stt_complete", 601_000_000)
    trace.mark("request_dispatched", 603_000_000)
    trace.mark("llm_first_token", 803_000_000)
    trace.mark("tts_submitted", 903_000_000)
    trace.mark("audio_ready", 1_003_000_000)
    threads = [Thread(target=lambda: trace.mark("device_playback", 1_013_000_000)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    report = metrics.report()
    assert report["speech_end_estimate_to_vad"]["p50_ms"] == 500
    assert report["vad_to_stt_wall"]["p50_ms"] == 100
    assert report["llm_ttft"]["p50_ms"] == 200
    assert report["audio_ready_to_device_playback"]["p50_ms"] == 10
    assert report["speech_end_estimate_to_device_playback"]["count"] == 1
    assert report["speech_end_estimate_to_device_playback"]["p50_ms"] == 1012


def test_league_trace_starts_at_detection_rather_than_later_notification_queue(monkeypatch):
    from core.context import Context
    from core.league_voice import LeagueAnnouncement
    from gui.workers import AdviceWorker
    metrics = LeagueMetrics(True)
    trace = latency.LatencyTrace(metrics)
    context = Context()
    monkeypatch.setattr(config, "LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    monkeypatch.setattr("gui.workers.start_trace", lambda: trace)
    monkeypatch.setattr(context.league, "announcement_current", lambda item: True)
    item = LeagueAnnouncement("Your team took Dragon.", "objective", time.monotonic(),
                              queued_ns=1_000_000, detected_ns=500_000)
    def speak(*args, **kwargs):
        trace.mark("device_playback", 2_000_000)
        kwargs["on_playback_start"]()
    monkeypatch.setattr(tts, "speak", speak)
    AdviceWorker([], context, item).run()
    assert metrics.report()["league_detected_to_device_playback"]["p50_ms"] == 1.5


def test_disabled_metrics_never_read_clocks(monkeypatch):
    monkeypatch.setattr(latency, "perf_counter_ns", lambda: pytest.fail("Disabled metric read the clock"))
    latency.LatencyTrace(LeagueMetrics(False)).mark("audio_ready")
    with latency.span("disabled"):
        pass


def test_model_transport_reuses_pool_preserves_stats_and_callback_thread(monkeypatch):
    real = ollama.AsyncClient
    clients, callback_threads = [], []
    caller = get_ident()
    def handler(request):
        body = (json.dumps({"message": {"role": "assistant", "content": "Ready."}, "done": True,
                            "eval_count": 5, "eval_duration": 100_000_000}) + "\n").encode()
        return httpx.Response(200, content=body)
    def client(**kwargs):
        result = real(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(result)
        return result
    monkeypatch.setattr(model_client.ollama, "AsyncClient", client)
    for _ in range(3):
        response = model_client.request_chat(model="fixture", messages=[], on_text=lambda text: callback_threads.append(get_ident()))
        assert response.eval_count == 5 and response.eval_duration == 100_000_000
    assert len(clients) == 1 and callback_threads == [caller] * 3
    thread = model_client._transport.thread
    model_client.shutdown()
    assert not thread.is_alive()


def test_foreground_stops_background_and_new_background_waits_without_blocking_caller(monkeypatch):
    monkeypatch.setattr(config, "INFERENCE_BACKGROUND_QUIET_SECONDS", .01)
    scheduler = InferencePriority()
    entered, preempted, resumed = Event(), Event(), Event()
    class Owner:
        pass
    owner = Owner()
    def background():
        with scheduler.background(Event()) as token:
            entered.set()
            assert token.wait(1)
            preempted.set()
        with scheduler.background(Event()):
            resumed.set()
    worker = Thread(target=background)
    worker.start()
    try:
        assert entered.wait(1)
        scheduler.user_started(owner)
        assert preempted.wait(1) and not resumed.wait(.03)
        scheduler.user_finished(owner)
        assert resumed.wait(1)
    finally:
        scheduler.user_finished(owner)
        worker.join(1)
    assert not worker.is_alive()


def test_background_admission_wait_is_cancellable():
    scheduler = InferencePriority()
    # A normal Python owner supports weak references; namespaces do not.
    class Owner:
        pass
    owner = Owner()
    scheduler.user_started(owner)
    stop, ended = Event(), Event()
    def wait():
        with pytest.raises(OperationCancelled):
            with scheduler.background(stop):
                pytest.fail("Background ran during user speech")
        ended.set()
    thread = Thread(target=wait)
    thread.start()
    stop.set()
    assert ended.wait(1)
    thread.join()
    scheduler.user_finished(owner)


def test_throughput_uses_server_tokens_and_reports_bounded_percentiles():
    collector = latency.VoiceMetrics(True)
    collector.record_tokens(50, 1_000_000_000)
    collector.record_tokens(100, 1_000_000_000)
    result = collector.report()["llm_decode_tokens_per_second"]
    assert result["count"] == 2 and result["p50_tokens_per_second"] == 50
    assert result["max_tokens_per_second"] == 100


def test_headphone_barge_in_cancels_old_reply_and_answers_new_input_without_restarting_worker(monkeypatch):
    from core.context import Context
    from gui.workers import VoiceWorker
    from voice import voice
    monkeypatch.setattr(config, "VOICE_BARGE_IN_ENABLED", True)
    monkeypatch.setattr(voice, "load_models", lambda *args: None)
    monkeypatch.setattr(tts, "warm_up", lambda *args: None)
    first_playing, interrupted, second_spoken = Event(), Event(), Event()
    recordings, spoken = [], []
    def record(cancellation, on_speech_start=None, **kwargs):
        index = len(recordings)
        recordings.append(index)
        if index == 1:
            assert first_playing.wait(2)
        elif index > 1:
            while not cancellation.wait(.01):
                pass
            check_cancelled(cancellation)
        on_speech_start()
        return index
    monkeypatch.setattr(voice, "record_until_silence", record)
    monkeypatch.setattr(voice, "transcribe_audio", lambda audio, **kwargs: "first" if audio == 0 else "second")
    monkeypatch.setattr(VoiceWorker, "_answer", lambda self, text: text + " answer")
    def speak(text, cancel_event, **kwargs):
        if text == "first answer":
            first_playing.set()
            try:
                while not cancel_event.wait(.01):
                    pass
                check_cancelled(cancel_event)
            finally:
                interrupted.set()
        else:
            spoken.append(text)
            second_spoken.set()
    monkeypatch.setattr(tts, "speak", speak)
    worker = VoiceWorker([], Context())
    monkeypatch.setattr(worker.context.interaction, "microphone_delay", lambda: pytest.fail("Headphone capture waited for speaker echo tail"))
    errors = []
    worker.error.connect(errors.append)
    worker.start()
    try:
        assert interrupted.wait(3) and second_spoken.wait(3)
        assert worker.isRunning() and spoken == ["second answer"] and not errors
    finally:
        worker.stop()
        assert worker.wait(3000)
    assert not worker._input_pump.thread.is_alive()


def test_capture_keeps_only_latest_complete_utterance():
    from voice.capture import CapturePump, Utterance
    pump = CapturePump(Event(), lambda: 1, lambda *args, **kwargs: None)
    for index in range(100):
        pump._publish(Utterance(index, index, 0))
    assert pump.queue.qsize() == 1
    assert pump.take(Event(), Event()).generation == 99


def test_background_http_is_cancelled_before_foreground_uses_same_pool(monkeypatch):
    priority = InferencePriority()
    monkeypatch.setattr(model_client, "inference_priority", priority)
    real = ollama.AsyncClient
    entered, closed, cancelled = Event(), Event(), Event()
    clients = []
    async def handler(request):
        if json.loads(request.content)["model"] == "background":
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                closed.set()
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "Ready."}})
    def client(**kwargs):
        result = real(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(result)
        return result
    monkeypatch.setattr(model_client.ollama, "AsyncClient", client)
    class User:
        pass
    user = User()
    def background():
        with pytest.raises(OperationCancelled):
            model_client.request_chat(model="background", messages=[], background=True)
        cancelled.set()
    thread = Thread(target=background)
    thread.start()
    try:
        assert entered.wait(1)
        priority.user_started(user)
        assert cancelled.wait(1) and closed.wait(1)
        assert model_client.request_chat(model="foreground", messages=[]).message.content == "Ready."
        assert len(clients) == 1
    finally:
        priority.user_started(user)  # Also releases the HTTP worker if an assertion fails.
        priority.user_finished(user)
        thread.join(1)


def test_native_failure_or_cancellation_cleans_partial_chunk_files(monkeypatch):
    from voice.kokoro_session import session
    paths = []
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    def synthesize(*args, **kwargs):
        path = kwargs["output"].with_name("first.wav")
        path.write_bytes(b"fixture")
        paths.append(path)
        kwargs["on_chunk"]({"chunk": str(path)})
    monkeypatch.setattr(session, "request", synthesize)
    def obsolete(chunk):
        raise OperationCancelled()
    with pytest.raises(OperationCancelled):
        tts.stream_speech("A reply interrupted during generation.", obsolete)
    assert paths and not paths[0].parent.exists()


def test_capture_failure_is_propagated_and_capture_thread_stops():
    from voice.capture import CapturePump
    def unavailable(*args, **kwargs):
        raise RuntimeError("Input device unavailable")
    pump = CapturePump(Event(), lambda: 1, unavailable)
    pump.start()
    try:
        with pytest.raises(RuntimeError, match="Input device unavailable"):
            pump.take(Event(), Event())
    finally:
        pump.close()
    assert not pump.thread.is_alive()


@pytest.mark.parametrize("requested,available,expected", [
    ("cpu", True, "cpu"), ("cuda", True, "cuda"), ("cuda", False, "cpu"),
    ("auto", True, "cuda"), ("auto", False, "cpu"),
])
def test_recognition_device_override_and_fallback_do_not_change_model(requested, available, expected, monkeypatch):
    from voice import voice
    probes, calls = [], []
    monkeypatch.setattr(voice, "WHISPER_DEVICE", requested)
    monkeypatch.setattr(voice, "whisper_model", None)
    monkeypatch.setattr(voice, "vad_iterator", None)
    monkeypatch.setattr(voice, "_dll_handles", [object()])
    def cuda_available():
        probes.append(True)
        return available
    def whisper(model, **options):
        calls.append((model, options))
        return object()
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=cuda_available)))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper))
    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(
        VADIterator=lambda *args, **kwargs: object(), load_silero_vad=lambda: object()))
    voice._load_models_locked(Event())
    assert calls[0] == (voice.WHISPER_MODEL, {"device": expected, "compute_type": "int8" if expected == "cpu" else "float16"})
    if requested == "cpu":
        assert not probes


def test_chat_preload_sends_no_prompt_or_audio_and_preserves_model_selection(monkeypatch):
    from core import conversation
    calls = []
    monkeypatch.setattr(conversation, "conversation_model", lambda: "qwen2.5:32b")
    monkeypatch.setattr(conversation, "request_chat", lambda **kwargs: calls.append(kwargs))
    stop = Event()
    conversation.warm_up(stop)
    assert calls == [{"cancel_event": stop, "model": "qwen2.5:32b", "messages": [],
                      "keep_alive": config.MODEL_KEEP_ALIVE, "background": True}]


def test_startup_preloads_chat_in_existing_worker_without_microphone_or_playback(monkeypatch):
    from gui.workers import VoiceWarmupWorker
    from core import conversation
    from voice import voice
    calls = []
    monkeypatch.setattr(config, "CONVERSATION_PRELOAD_ON_STARTUP", True)
    monkeypatch.setattr(voice, "load_models", lambda token: calls.append("stt"))
    monkeypatch.setattr(tts, "warm_up", lambda token: calls.append("tts"))
    monkeypatch.setattr(conversation, "warm_up", lambda token: calls.append("chat"))
    monkeypatch.setattr(voice, "record_until_silence", lambda *args, **kwargs: pytest.fail("Warm-up opened microphone"))
    monkeypatch.setattr(tts, "_play_wav", lambda *args, **kwargs: pytest.fail("Warm-up played audio"))
    VoiceWarmupWorker().run()
    assert calls == ["stt", "tts", "chat"]


def test_output_device_is_reused_across_chunks_and_closed_once(monkeypatch):
    import sounddevice
    from voice.playback import playback_session, play_samples
    devices, rendered = [], []
    class Output:
        active = True
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.closed = Event()
            self.thread = Thread(target=self.render)
            devices.append(self)
        def start(self):
            self.thread.start()
        def render(self):
            while not self.closed.wait(.001):
                block = np.empty((240, 1), dtype="int16")
                self.callback(block, 240, SimpleNamespace(outputBufferDacTime=1, currentTime=1), None)
                rendered.extend(block[:, 0][block[:, 0] != 0].tolist())
        def stop(self):
            self.active = False
        abort = stop
        def close(self):
            self.closed.set()
            self.thread.join(1)
    monkeypatch.setattr(sounddevice, "OutputStream", Output)
    starts = []
    with playback_session(Event()):
        play_samples(np.full((480, 1), 100, dtype="int16"), 24000, on_started=lambda: starts.append(True))
        play_samples(np.full((480, 1), 200, dtype="int16"), 24000, on_started=lambda: starts.append(True))
    assert len(devices) == 1 and starts == [True, True]
    assert rendered == [100] * 480 + [200] * 480
    assert devices[0].closed.is_set() and not devices[0].thread.is_alive()


def test_first_complete_clause_is_submitted_before_long_sentence_finishes():
    ready = Event()
    parts = []
    reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
        speak=lambda text: parts.append(text) or ready.set(), min_segment_chars=32, first_chunk_max_chars=80)
    try:
        reply.feed("Keep your answer grounded in what we know, ")
        assert ready.wait(1)
        reply.feed("and explain the useful part first.")
        reply.finish()
        assert parts == ["Keep your answer grounded in what we know,", "and explain the useful part first."]
    finally:
        reply.abort()


def test_text_queue_is_bounded_and_abort_releases_blocked_model_feed():
    playing, ended = Event(), Event()
    def play(text):
        playing.set()
        reply.cancellation.wait(2)
        check_cancelled(reply.cancellation)
    reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
                           speak=play, text_queue_limit=2)
    def feed():
        try:
            reply.feed("One useful sentence. " * 1000)
        except OperationCancelled:
            ended.set()
    thread = Thread(target=feed)
    thread.start()
    assert playing.wait(1) and reply._queue.maxsize == 2 and reply._queue.qsize() <= 2
    reply.abort()
    assert ended.wait(1)
    thread.join(1)
    assert not thread.is_alive() and not reply._thread.is_alive()


def test_native_chunks_keep_order_and_do_not_suppress_their_own_continuation():
    spoken, closed = [], []
    interaction = InteractionState()
    class Chunk:
        def __init__(self, number):
            self.number = number
        def close(self):
            closed.append(self.number)
    def prepare(text, ready):
        for number in range(3):
            ready(Chunk(number))
    def play(chunk):
        spoken.append(chunk.number)
        interaction.record_spoken("A sufficiently long complete sentence.")
    reply = StreamingReply(Event(), interaction, "Explain", lambda text: None, speak=play,
                           prepare_stream=prepare, discard=lambda chunk: chunk.close())
    try:
        reply.feed("A sufficiently long complete sentence.")
        reply.finish()
        assert spoken == [0, 1, 2] and closed == [0, 1, 2]
    finally:
        reply.abort()


def test_native_chunks_arrive_before_full_synthesis_and_cleanup_waits_for_playback(monkeypatch):
    from voice.kokoro_session import session
    import wave
    queued, generated = [], []
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    def synthesize(*args, **kwargs):
        for index in range(2):
            path = kwargs["output"].with_name(f"chunk-{index}.wav")
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\1\0" * 240)
            kwargs["on_chunk"]({"chunk": str(path)})
            assert len(queued) == index + 1  # No wait for the complete return value.
        generated.append(True)
    monkeypatch.setattr(session, "request", synthesize)
    tts.stream_speech("Hey, chum. I'm right here.", queued.append)
    assert generated and all(chunk.path.exists() for chunk in queued)
    directory = queued[0].path.parent
    for chunk in queued:
        chunk.close()
    assert not directory.exists()


def test_device_callback_copies_ordered_chunks_and_reports_dac_time_not_queue_time():
    player = PlaybackSession()
    player.rate = 24000
    from voice.playback import _Job
    first = _Job(np.full((6, 1), 100, dtype="int16"), Event())
    second = _Job(np.full((6, 1), 200, dtype="int16"), Event())
    player.jobs.extend((first, second))
    block = np.zeros((12, 1), dtype="int16")
    before = time.perf_counter_ns()
    player._callback(block, 12, SimpleNamespace(outputBufferDacTime=1.04, currentTime=1), None)
    assert block[:, 0].tolist() == [100] * 6 + [200] * 6
    assert first.submitted.is_set() and second.submitted.is_set()
    assert first.start_ns >= before + 39_000_000
    assert second.start_ns - first.start_ns == 250_000


def test_device_callback_drops_cancelled_audio_before_copying():
    from voice.playback import _Job
    stop = Event()
    stop.set()
    job = _Job(np.full((5, 1), 1000, dtype="int16"), stop)
    player = PlaybackSession()
    player.rate = 24000
    player.jobs.append(job)
    block = np.empty((5, 1), dtype="int16")
    player._callback(block, 5, SimpleNamespace(outputBufferDacTime=1, currentTime=1), None)
    assert not block.any() and job.submitted.is_set() and job.start_ns is None

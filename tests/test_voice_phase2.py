import io
import json
import wave
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import numpy as np
import ollama
import pytest

import config
from core import latency, model_client
from core.cancellation import OperationCancelled
from core.context import Context
from core.league_metrics import LeagueMetrics
from core.streaming import StreamingReply
from core.interaction import InteractionState
from core.inference_priority import InferencePriority
from gui.workers import VoiceWorker
from PySide6.QtCore import Qt
from voice import voice
from voice.kokoro_session import KokoroSession
from voice.playback import PlaybackSession


def test_tts_queue_synthesis_pcm_and_dac_are_distinct_metrics():
    collector = LeagueMetrics(True)
    trace = latency.LatencyTrace(collector)
    for name, milliseconds in (("llm_first_token", 0), ("tts_queued", 20), ("tts_submitted", 30),
                               ("tts_worker_dispatched", 35), ("audio_ready", 200), ("pcm_ready", 203),
                               ("device_submitted", 210), ("device_playback", 230),
                               ("interruption_detected", 300), ("output_abort_complete", 310),
                               ("interrupted_turn_stopped", 350)):
        trace.mark(name, milliseconds * 1_000_000)
    report = collector.report()
    expected = {"first_text_to_tts_queue": 20, "tts_queue_wait": 10, "tts_setup_and_lock_wait": 5,
                "tts_submit_to_first_audio_ready": 170, "audio_ready_to_pcm_ready": 3,
                "pcm_ready_to_device_submission": 7, "device_submission_to_dac_estimate": 20,
                "interruption_to_output_abort": 10, "interruption_to_turn_cleanup": 50}
    for name, value in expected.items():
        assert report[name]["p50_ms"] == value and report[name]["count"] == 1


def test_stream_queue_mark_does_not_pretend_synthesis_has_started():
    collector = LeagueMetrics(True)
    trace = latency.LatencyTrace(collector)
    trace.mark("llm_first_token")
    with latency.trace_scope(trace):
        reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None, speak=lambda text: None)
        try:
            reply.feed("An ordered complete sentence. ")
            reply.finish()
        finally:
            reply.abort()
    assert "first_text_to_tts_queue" in collector.report()
    assert "first_token_to_tts_submit" not in collector.report()


def test_whitespace_stream_fragments_do_not_count_as_first_useful_text(monkeypatch):
    real = ollama.AsyncClient
    class Trace:
        collector = LeagueMetrics(True)
        def __init__(self):
            self.useful_marks = 0
        def mark(self, name):
            if name == "llm_first_token":
                self.useful_marks += 1
    trace, counts = Trace(), []
    body = "".join(json.dumps({"message": {"role": "assistant", "content": text}, "done": done}) + "\n"
                   for text, done in ((" \n", False), ("Useful text.", True)))
    monkeypatch.setattr(model_client.ollama, "AsyncClient", lambda **kwargs: real(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))))
    with latency.trace_scope(trace):
        response = model_client.request_chat(model="fixture", messages=[],
                                             on_text=lambda text: counts.append(trace.useful_marks))
    assert counts == [0, 1] and response.message.content == " \nUseful text."


@pytest.mark.parametrize("enabled", [True, False])
def test_recognition_text_only_option_preserves_existing_filters(monkeypatch, enabled):
    calls = []
    def transcribe(audio, **kwargs):
        calls.append(kwargs)
        return iter([SimpleNamespace(text=" A useful answer.")]), None
    monkeypatch.setattr(voice, "whisper_model", SimpleNamespace(transcribe=transcribe))
    monkeypatch.setattr(config, "WHISPER_WITHOUT_TIMESTAMPS", enabled)
    assert voice.transcribe_audio(np.zeros(512, dtype=np.float32), Event()) == "A useful answer."
    assert calls[0]["without_timestamps"] is enabled
    assert calls[0]["vad_filter"] is True and calls[0]["condition_on_previous_text"] is False


def fake_warm_session(monkeypatch):
    session = KokoroSession()
    session.process = SimpleNamespace(stdin=io.StringIO())
    session._signature = ("warm",)
    session._messages = Queue()
    shutdowns = []
    monkeypatch.setattr(session, "_prepare_locked", lambda *args: None)
    monkeypatch.setattr(session, "_shutdown_locked", lambda: shutdowns.append(True))
    return session, shutdowns


def test_cancelled_request_keeps_warm_worker_when_completion_is_already_queued(monkeypatch, tmp_path):
    session, shutdowns = fake_warm_session(monkeypatch)
    cancellation, chunks = Event(), []
    session._messages.put({"chunk": "first", "generation_complete": True})
    session._messages.put({"chunk": "obsolete"})
    session._messages.put({"ok": True})
    def ready(message):
        chunks.append(message)
        cancellation.set()
    with pytest.raises(OperationCancelled):
        session.request(tmp_path, cancellation, text="A complete sentence.", output=tmp_path / "reply.wav", on_chunk=ready)
    assert not shutdowns and chunks == [{"chunk": "first", "generation_complete": True}] and session._messages.empty()
    session._messages.put({"ok": True})
    session.request(tmp_path, Event(), text="The next answer.", output=tmp_path / "next.wav")
    assert not shutdowns and session._messages.empty() and session.process.stdin.getvalue().count("\n") == 2


@pytest.mark.parametrize("final", [None, {"error": "fixture"}, {"ready": True}])
def test_cancellation_terminates_incomplete_or_broken_worker_after_bounded_drain(monkeypatch, tmp_path, final):
    session, shutdowns = fake_warm_session(monkeypatch)
    cancellation = Event()
    session._messages.put({"chunk": "first"})
    if final:
        session._messages.put(final)
    monkeypatch.setattr(config, "TTS_CANCEL_DRAIN_SECONDS", .001)
    with pytest.raises(OperationCancelled):
        session.request(tmp_path, cancellation, text="A complete sentence.", output=tmp_path / "reply.wav",
                        on_chunk=lambda message: cancellation.set())
    assert shutdowns == [True]


def test_cancellation_before_request_never_reinitializes_or_discards_idle_voice(monkeypatch, tmp_path):
    session, shutdowns = fake_warm_session(monkeypatch)
    cancellation = Event()
    cancellation.set()
    monkeypatch.setattr(session, "_prepare_locked", lambda *args: pytest.fail("Cancelled request loaded a model"))
    with pytest.raises(OperationCancelled):
        session.request(tmp_path, cancellation)
    assert not shutdowns and session.process.stdin.getvalue() == ""


def test_incomplete_native_chunk_never_uses_completion_grace(monkeypatch, tmp_path):
    session, shutdowns = fake_warm_session(monkeypatch)
    cancellation = Event()
    session._messages.put({"chunk": "first", "generation_complete": False})
    session._messages.put({"ok": True})
    with pytest.raises(OperationCancelled):
        session.request(tmp_path, cancellation, text="A continuing sentence.", output=tmp_path / "reply.wav",
                        on_chunk=lambda message: cancellation.set())
    assert shutdowns == [True]  # No claim of completion from a partial chunk.


def test_native_worker_marks_only_its_actual_final_chunk(tmp_path):
    from voice.kokoro_worker import synthesize
    values = np.full(240, .1, dtype=np.float32)
    def generate(text, callback, **kwargs):
        callback(values, .5)
        callback(values, 1.0)
        return SimpleNamespace(samples=np.concatenate((values, values)), sample_rate=24000)
    engine = SimpleNamespace(num_speakers=54, sample_rate=24000, generate=generate)
    chunks = []
    synthesize(engine, {"text": "An ordered test.", "output": tmp_path / "reply.wav"}, chunks.append)
    assert [chunk["generation_complete"] for chunk in chunks] == [False, True]
    assert all(chunk["frames"] == 240 for chunk in chunks)


def test_followup_grace_defers_background_but_expires_normally(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.inference_priority.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(config, "INFERENCE_BACKGROUND_QUIET_SECONDS", 6)
    scheduler = InferencePriority()
    class User:
        pass
    owner = User()
    scheduler.user_started(owner)
    scheduler.user_finished(owner)
    clock[0] = 103
    assert not scheduler.background_ready()
    clock[0] = 106.01
    assert scheduler.background_ready()


def test_late_microphone_callback_after_mute_does_not_cancel_speaking_turn():
    worker = VoiceWorker([], Context())
    worker._turn_active = True
    worker._playback_active = True
    worker.mute_microphone()
    assert worker._barge_in_started() == 0
    assert not worker._turn_cancel.is_set() and worker._playback_active


def test_playback_start_and_cancel_signals_cannot_overtake_each_other():
    worker = VoiceWorker([], Context())
    entered, release, cancelling, cancelled = Event(), Event(), Event(), Event()
    observed = []
    def hold_start(active):
        if active:
            entered.set()
            assert release.wait(2)
    worker.playback_active_changed.connect(hold_start, Qt.ConnectionType.DirectConnection)
    worker.playback_active_changed.connect(observed.append, Qt.ConnectionType.DirectConnection)
    start = Thread(target=lambda: worker._set_playback_active(True))
    def stop():
        cancelling.set()
        worker._set_playback_active(False)
        cancelled.set()
    cancel = Thread(target=stop)
    start.start()
    try:
        assert entered.wait(1)
        cancel.start()
        assert cancelling.wait(1) and not cancelled.wait(.02)
    finally:
        release.set()
        start.join(2)
        if cancel.ident is not None:
            cancel.join(2)
    assert observed == [True, False] and not worker._playback_active


def test_barge_in_and_cleanup_measure_the_cancelled_turn_not_the_next_turn():
    worker = VoiceWorker([], Context())
    collector = LeagueMetrics(True)
    worker._trace = latency.LatencyTrace(collector)
    incoming = latency.LatencyTrace(collector)
    worker._turn_active = True
    with latency.trace_scope(incoming):
        worker._barge_in_started()
        latency.mark("stt_started")
    assert worker._turn_cancel.is_set()
    worker._finish_spatial()
    assert collector.report()["interruption_to_turn_cleanup"]["count"] == 1 and worker._trace is None
    assert collector.report()["interruption_to_new_stt_start"]["count"] == 1


def test_output_abort_metric_fires_only_after_driver_abort_returns():
    collector = LeagueMetrics(True)
    trace = latency.LatencyTrace(collector)
    calls = []
    def abort():
        assert "interruption_to_output_abort" not in collector.report()
        calls.append("abort")
    player = PlaybackSession()
    player.stream = SimpleNamespace(abort=abort, close=lambda: calls.append("close"))
    trace.mark("interruption_detected")
    with latency.trace_scope(trace):
        player.close(abort=True)
    assert calls == ["abort", "close"] and collector.report()["interruption_to_output_abort"]["count"] == 1


def test_recording_loader_preserves_stereo_average_and_rejects_other_encodings(tmp_path):
    from benchmark_voice import read_recording
    path = tmp_path / "fixture.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(np.array([[1000, 3000], [-2000, -4000]], dtype="<i2").tobytes())
    assert read_recording(path) == pytest.approx(np.array([2000, -3000]) / 32768)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 1, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0")
    with pytest.raises(ValueError, match="16-bit"):
        read_recording(path)


@pytest.mark.parametrize("companion", [False, True])
@pytest.mark.parametrize("warmups", [0, 1])
def test_league_replay_harness_needs_no_ollama_audio_device_or_live_client(qapp, monkeypatch, companion, warmups):
    import benchmark_voice
    import gui.window as gui
    from core import conversation
    from voice import tts
    monkeypatch.setattr(config, "LEAGUE_CHAMP_SELECT_ENABLED", True)
    monkeypatch.setattr(voice, "load_models", lambda *args: None)
    monkeypatch.setattr(voice, "transcribe_audio", lambda *args: "What role did I get?")
    monkeypatch.setattr(tts, "warm_up", lambda: None)
    monkeypatch.setattr(conversation, "conversation_model", lambda: pytest.fail("Deterministic benchmark selected an LLM"))
    monkeypatch.setattr(conversation, "request_chat", lambda **kwargs: pytest.fail("Deterministic benchmark used Ollama"))
    windows = []
    original_window = gui.CapriWindow
    def make_window(*args):
        windows.append(True)
        return original_window(*args)
    monkeypatch.setattr(gui, "CapriWindow", make_window)
    class Detector:
        def reset_states(self):
            self.steps = 0
        def __call__(self, *args, **kwargs):
            self.steps += 1
            return {"end": 4000} if self.steps == 20 else None
    monkeypatch.setattr(voice, "vad_iterator", Detector())
    def synthesize(text, output, *args, **kwargs):
        with wave.open(str(output), "wb") as target:
            target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            target.writeframes(np.full(1600, 1000, dtype="<i2").tobytes())
    monkeypatch.setattr(tts, "_synthesize", synthesize)
    original_play = tts._play_wav
    result = benchmark_voice.replay_bench(2, "fixture", league_fixture=True, companion=companion, warmup_turns=warmups)
    assert result["model"] == "none (deterministic API fixture)"
    assert result["metrics"]["fixture_speech_end_to_first_audio_ready"]["count"] == 2
    assert len(windows) == int(companion)
    assert tts._play_wav is original_play

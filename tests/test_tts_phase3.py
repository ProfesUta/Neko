import sys
import os
import wave
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

import config
from core.latency import LatencyTrace, VoiceMetrics, trace_scope
from core.interaction import InteractionState
from core.streaming import StreamingReply
from voice import kokoro_worker
from voice.kokoro_session import KokoroSession, _record_native_timing
from voice.playback import PlaybackSession
from tests.test_kokoro_session import assets


def test_native_profile_separates_callback_conversion_file_and_generate_without_changing_pcm(tmp_path):
    values = np.array([-.5, 0, .5], dtype=np.float32)
    def generate(text, **options):
        options["callback"](values, 1.0)
        return SimpleNamespace(samples=values, sample_rate=24000)
    engine = SimpleNamespace(generate=generate, num_speakers=54, sample_rate=24000)
    messages = []
    result = kokoro_worker.synthesize(engine, {"text": "A fixed phrase.", "output": tmp_path / "sample.wav", "profile": True}, messages.append)
    timing = messages[0]["timing"]
    assert timing["generate_enter_ns"] <= timing["callback_ns"] <= timing["pcm_ns"] <= timing["wav_ns"]
    assert result["first_callback_cpu_ns"] >= 0
    assert result["timing"]["generate_return_ns"] >= timing["callback_ns"]
    import wave
    with wave.open(messages[0]["chunk"], "rb") as source:
        assert np.frombuffer(source.readframes(3), dtype="<i2").tolist() == [-16383, 0, 16383]


def test_unprofiled_native_synthesis_never_reads_profile_clocks(tmp_path, monkeypatch):
    def forbidden():
        pytest.fail("Unprofiled generation read a clock")
    monkeypatch.setattr(kokoro_worker, "time", SimpleNamespace(perf_counter_ns=forbidden, process_time_ns=forbidden))
    values = np.array([.1, .2], dtype=np.float32)
    engine = SimpleNamespace(num_speakers=54, sample_rate=24000)
    def generate(text, callback, **options):
        callback(values, 1)
        return SimpleNamespace(samples=values, sample_rate=24000)
    engine.generate = generate
    chunks = []
    result = kokoro_worker.synthesize(engine, {"text": "A fixed phrase.", "output": tmp_path / "test.wav"}, chunks.append)
    assert "timing" not in result and "timing" not in chunks[0]


def test_parent_native_timing_reports_real_stage_differences():
    collector = VoiceMetrics(True)
    trace = LatencyTrace(collector)
    trace.mark("tts_worker_dispatched", 1_000_000)
    _record_native_timing(trace, {"timing": {"generate_enter_ns": 2_000_000, "callback_ns": 102_000_000,
                                           "pcm_ns": 103_000_000, "wav_ns": 104_000_000}, "received_ns": 106_000_000})
    report = collector.report()
    for key, expected in (("tts_dispatch_to_native_generate", 1), ("native_generate_to_first_callback", 100),
                          ("native_callback_to_pcm", 1), ("native_pcm_to_wav", 1), ("native_wav_to_parent_receive", 2)):
        assert report[key]["p50_ms"] == expected and report[key]["count"] == 1


def test_cpu_options_participate_in_worker_signature_and_missing_options_fall_back(assets, tmp_path, monkeypatch, caplog):
    path = tmp_path / "cpu.conf"
    path.write_text("SessionConfig.session.dynamic_block_base=4\n")
    monkeypatch.setattr(config, "KOKORO_CPU_OPTIONS", str(path))
    session = KokoroSession()
    files, arguments = session._resources(assets)
    assert path.resolve() in files and arguments[-1] == str(path.resolve())
    path.unlink()
    files, arguments = session._resources(assets)
    session._resources(assets)
    assert path not in files and len(arguments) == 2
    assert caplog.text.count("CPU options unavailable") == 1


def test_pcm_is_ready_for_callbacks_executing_inside_device_start(monkeypatch):
    import sounddevice
    blocks, starts = [], []
    class Output:
        active = True
        def __init__(self, **options):
            self.callback = options["callback"]
        def start(self):
            output = np.empty((240, 1), dtype="int16")
            self.callback(output, 240, SimpleNamespace(outputBufferDacTime=1, currentTime=1), None)
            blocks.append(output.copy())
        def stop(self):
            self.active = False
        abort = stop
        def close(self):
            pass
    monkeypatch.setattr(sounddevice, "OutputStream", Output)
    player = PlaybackSession()
    try:
        player.play(np.full((240, 1), 1000, dtype="int16"), 24000, Event(), lambda: starts.append(True))
    finally:
        player.close()
    assert blocks[0][:, 0].tolist() == [1000] * 240 and starts == [True]


def test_output_profile_aggregates_only_outside_the_audio_callback():
    collector = VoiceMetrics(True)
    trace = LatencyTrace(collector)
    player = PlaybackSession()
    player.rate = 24000
    player._trace = trace
    player._callback(np.empty((240, 1), dtype="int16"), 240,
                     SimpleNamespace(outputBufferDacTime=1, currentTime=1), SimpleNamespace(output_underflow=True))
    assert collector.report() == {}  # No aggregation locks on the device callback.
    player.stream = SimpleNamespace(abort=lambda: None, close=lambda: None)
    with trace_scope(trace):
        player.close(abort=True)
    counters = collector.report()["counters"]
    assert counters["output_underflows"] == 1 and counters["output_empty_start_callbacks"] == 1


def test_cpu_tuning_failure_retries_original_cpu_provider(assets, tmp_path, monkeypatch):
    path = tmp_path / "cpu.conf"
    path.write_text("SessionConfig.session.dynamic_block_base=4\n")
    received = []
    def build(**values):
        return SimpleNamespace(**values)
    def configuration(**values):
        return SimpleNamespace(**values, validate=lambda: True)
    def engine(settings):
        received.append(settings.model.provider)
        if settings.model.provider != "cpu":
            raise RuntimeError("Unavailable session option")
        return settings
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(__version__="2.0.1",
        OfflineTtsKokoroModelConfig=build, OfflineTtsModelConfig=build, OfflineTtsConfig=configuration, OfflineTts=engine))
    kokoro_worker.create_tts(assets, 4, path)
    assert received == ["cpu:" + str(path.resolve()), "cpu"]


@pytest.mark.parametrize("version", ["1.12.29", "unknown"])
def test_older_native_runtime_retains_original_cpu_provider(assets, tmp_path, monkeypatch, version):
    path = tmp_path / "cpu.conf"
    path.write_text("SessionConfig.session.dynamic_block_base=4\n")
    def build(**values):
        return SimpleNamespace(**values)
    def configuration(**values):
        return SimpleNamespace(**values, validate=lambda: True)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(__version__=version,
        OfflineTtsKokoroModelConfig=build, OfflineTtsModelConfig=build,
        OfflineTtsConfig=configuration, OfflineTts=lambda settings: settings))
    assert kokoro_worker.create_tts(assets, 8, path).model.provider == "cpu"


def test_nonstreaming_profile_does_not_invent_a_native_callback(tmp_path):
    values = np.array([.1, .2], dtype=np.float32)
    engine = SimpleNamespace(num_speakers=54, sample_rate=24000,
                             generate=lambda *args, **options: SimpleNamespace(samples=values, sample_rate=24000))
    result = kokoro_worker.synthesize(engine, {"text": "A fixed phrase.", "output": tmp_path / "full.wav", "profile": True})
    assert "first_callback_cpu_ns" not in result and "callback_ns" not in result["timing"]
    timing = result["timing"]
    assert timing["generate_enter_ns"] <= timing["generate_return_ns"] <= timing["pcm_ns"] <= timing["wav_ns"]


@pytest.mark.parametrize("first_min,expected", [(0, []), (20, ["Oh, you picked Yuumi?"])])
def test_only_complete_first_sentence_gets_the_earlier_boundary(first_min, expected):
    submitted = []
    ready = Event()
    reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
                           speak=lambda text: submitted.append(text) or ready.set(),
                           min_segment_chars=32, first_sentence_min_chars=first_min)
    try:
        reply.feed("Oh, you picked Yuumi? ")
        if expected:
            assert ready.wait(1) and submitted == expected
        else:
            assert not ready.is_set()
        reply.feed("I see how it is, chum. ")
        reply.finish()
        assert " ".join(submitted) == "Oh, you picked Yuumi? I see how it is, chum."
    finally:
        reply.abort()


def test_short_first_sentence_boundary_does_not_cut_abbreviations_decimals_or_filler():
    submitted = []
    reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
                           speak=submitted.append, min_segment_chars=32, first_sentence_min_chars=20)
    try:
        reply.feed("Well... Dr. Capri measured 2.5 ")
        assert submitted == []
        reply.feed("units. ")
        reply.finish()
        assert submitted[0] == "Well... Dr. Capri measured 2.5 units."
    finally:
        reply.abort()


def test_tts_benchmark_saves_actual_ordered_pcm_without_devices(tmp_path, monkeypatch):
    import benchmark_tts
    monkeypatch.setattr(config, "KOKORO_CPU_OPTIONS", "")
    monkeypatch.setattr(config, "KOKORO_NUM_THREADS", 4)
    monkeypatch.setattr(benchmark_tts.session, "shutdown", lambda: None)
    monkeypatch.setattr(benchmark_tts.session, "process", SimpleNamespace(pid=os.getpid()))
    monkeypatch.setattr(benchmark_tts.tts, "warm_up", lambda: None)
    closed = []
    def stream(text, ready):
        for number in (1, 2):
            path = tmp_path / f"chunk-{number}.wav"
            with wave.open(str(path), "wb") as target:
                target.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                target.writeframes(np.full(64, number * 100, dtype="<i2").tobytes())
            ready(SimpleNamespace(path=path, close=lambda: closed.append(True)))
    monkeypatch.setattr(benchmark_tts.tts, "stream_speech", stream)
    result = benchmark_tts.measure(1, 4, ["short"], save=tmp_path)
    with wave.open(str(tmp_path / "short.wav"), "rb") as source:
        samples = np.frombuffer(source.readframes(128), dtype="<i2")
    assert samples.tolist() == [100] * 64 + [200] * 64
    assert len(closed) == 4 and result["fixtures"]["short"]["metrics"]["text_to_first_usable_chunk"]["count"] == 1


def test_benchmark_plain_control_does_not_override_requested_candidate(tmp_path, monkeypatch):
    import benchmark_tts
    import benchmark_voice
    import importlib.metadata
    from core import model_client
    options = tmp_path / "cpu.conf"
    options.write_text("SessionConfig.session.dynamic_block_base=4\n")
    monkeypatch.setattr(config, "KOKORO_NUM_THREADS", 8)
    monkeypatch.setattr(config, "KOKORO_CPU_OPTIONS", str(options))
    monkeypatch.setattr(benchmark_voice, "hardware", lambda: {})
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "fixture")
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(onnxruntime_version="fixture"))
    monkeypatch.setattr(benchmark_tts.session, "shutdown", lambda: None)
    monkeypatch.setattr(model_client, "shutdown", lambda: None)
    calls = []
    def measure(*args):
        calls.append((args[1], str(args[3])))
        config.KOKORO_CPU_OPTIONS = args[3]
        return {}
    monkeypatch.setattr(benchmark_tts, "measure", measure)
    monkeypatch.setattr(sys, "argv", ["benchmark_tts.py", "--samples", "1", "--plain-control", "--output", str(tmp_path / "result.json")])
    benchmark_tts.main()
    assert calls == [(4, ""), (8, str(options))]

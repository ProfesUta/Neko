import importlib
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

import config
from core import latency
from core.cancellation import OperationCancelled
from core.latency import LatencyTrace, VoiceMetrics, trace_scope
from voice import playback, voice
from tests.test_voice import recording_stubs


def test_capture_completion_includes_assembly_and_retains_preroll(monkeypatch):
    recording_stubs(monkeypatch)
    collector = VoiceMetrics(True)
    trace = LatencyTrace(collector)
    with trace_scope(trace):
        audio = voice.record_until_silence()
    assert len(audio) == 14 * 512
    assert trace._marks["vad_detected"] <= trace._marks["capture_stopped"] <= trace._marks["capture_complete"]
    assert collector.report()["capture_assembly_wall"]["count"] == 1


def test_recognition_frontend_and_lazy_decoder_are_distinct_and_filters_preserved(monkeypatch):
    now, received = [1_000_000_000], []
    monkeypatch.setattr(latency, "perf_counter_ns", lambda: now[0])
    def transcribe(audio, **options):
        received.append(options)
        now[0] += 20_000_000
        def segments():
            now[0] += 80_000_000
            yield SimpleNamespace(text=" Complete words.")
        return segments(), None
    monkeypatch.setattr(voice, "whisper_model", SimpleNamespace(transcribe=transcribe))
    metrics = VoiceMetrics(True)
    with trace_scope(LatencyTrace(metrics)):
        assert voice.transcribe_audio(np.zeros(512), Event()) == "Complete words."
    assert metrics.report()["stt_frontend_wall"]["p50_ms"] == 20
    assert metrics.report()["stt_decode_wall"]["p50_ms"] == 80
    assert metrics.report()["stt_wall"]["p50_ms"] == 100
    assert received[0]["vad_filter"] is True


@pytest.mark.parametrize("threads,expected", [(0, 0), (8, 8), (99, 16)])
def test_recognizer_thread_setting_is_bounded_and_model_reused(monkeypatch, threads, expected):
    import sys
    calls = []
    def model(*args, **kwargs):
        calls.append(kwargs)
        return object()
    monkeypatch.setattr(voice, "whisper_model", None)
    monkeypatch.setattr(voice, "vad_iterator", object())
    monkeypatch.setattr(voice, "_dll_handles", [object()])
    monkeypatch.setattr(voice, "WHISPER_DEVICE", "cpu")
    monkeypatch.setattr(config, "WHISPER_CPU_THREADS", threads)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=model))
    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(VADIterator=None, load_silero_vad=None))
    voice.load_models()
    original = voice.whisper_model
    voice.load_models()
    assert voice.whisper_model is original and len(calls) == 1
    assert calls[0].get("cpu_threads", 0) == expected and calls[0]["compute_type"] == "int8"


def fake_devices(monkeypatch, duplicate=False):
    monkeypatch.setattr(playback, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(config, "TTS_WASAPI_ENABLED", True)
    devices = [{"hostapi": 0, "name": "Speakers (Test audio", "max_output_channels": 2},
               {"hostapi": 1, "name": "Speakers (Test audio device)", "max_output_channels": 2}]
    if duplicate:
        devices.append(dict(devices[1]))
    return SimpleNamespace(query_devices=lambda **kwargs: devices[0] if kwargs else devices,
        query_hostapis=lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
        WasapiSettings=lambda **options: options, PortAudioError=RuntimeError), devices


def test_same_endpoint_wasapi_match_is_unique_and_does_not_change_global_device(monkeypatch):
    sd, devices = fake_devices(monkeypatch)
    assert playback._output_options(sd) == {"device": 1, "extra_settings": {"auto_convert": True}}
    devices[0]["name"] = "Another selected device"
    assert playback._output_options(sd) == {}  # Re-resolve after a selection change.


def test_ambiguous_endpoint_names_and_disabled_preference_keep_original_route(monkeypatch):
    sd, devices = fake_devices(monkeypatch, duplicate=True)
    assert playback._output_options(sd) == {}
    monkeypatch.setattr(config, "TTS_WASAPI_ENABLED", False)
    sd.query_devices = lambda **kwargs: pytest.fail("Disabled preference queried audio devices")
    assert playback._output_options(sd) == {}


@pytest.mark.parametrize("failure", ["open", "start", "after_samples"])
def test_driver_failure_falls_back_only_before_any_pcm_is_consumed(monkeypatch, failure):
    import sounddevice as sd
    calls, closed = [], []
    monkeypatch.setattr(playback, "_output_options", lambda sd: {"device": 7})
    class Output:
        active = True
        def __init__(self, **options):
            calls.append(options)
            self.options = options
            if options.get("device") == 7 and failure == "open":
                raise sd.PortAudioError("Unsupported shared format")
        def start(self):
            preferred = self.options.get("device") == 7
            if preferred and failure == "start":
                raise sd.PortAudioError("Device busy")
            self.options["callback"](np.zeros((240, 1), dtype="int16"), 240,
                SimpleNamespace(outputBufferDacTime=1, currentTime=1), None)
            if preferred and failure == "after_samples":
                raise sd.PortAudioError("Started then failed")
        def close(self):
            closed.append(self.options.get("device"))
        def stop(self):
            self.active = False
        abort = stop
    monkeypatch.setattr(sd, "OutputStream", Output)
    player = playback.PlaybackSession()
    try:
        if failure == "after_samples":
            with pytest.raises(sd.PortAudioError):
                player.play(np.full((240, 1), 1000, dtype="int16"), 24000, Event(), None)
            assert len(calls) == 1  # Never repeat speech after partial submission.
        else:
            player.play(np.full((240, 1), 1000, dtype="int16"), 24000, Event(), None)
            assert len(calls) == 2 and "device" not in calls[1]
    finally:
        player.close(abort=True)
    assert closed


def test_pcm_callback_dac_and_loopback_timestamps_are_separate_metrics():
    collector = VoiceMetrics(True)
    trace = LatencyTrace(collector)
    for name, ms in (("speech_end_estimate", 0), ("pcm_ready", 100), ("playback_queued", 101),
                     ("device_first_callback", 110), ("device_submitted", 150),
                     ("device_playback", 170), ("loopback_onset", 190), ("loopback_packet_received", 198)):
        trace.mark(name, ms * 1_000_000)
    result = collector.report()
    assert result["pcm_to_playback_queue"]["p50_ms"] == 1
    assert result["playback_queue_to_first_callback"]["p50_ms"] == 9
    assert result["pcm_ready_to_dac_estimate"]["p50_ms"] == 70
    assert result["speech_end_estimate_to_loopback_onset"]["p50_ms"] == 190
    assert result["loopback_packet_delivery"]["p50_ms"] == 8


@pytest.mark.parametrize("failure", ["open", "start"])
def test_cancellation_during_driver_failure_does_not_reopen_obsolete_output(monkeypatch, failure):
    import sounddevice as sd
    cancellation, attempts = Event(), []
    monkeypatch.setattr(playback, "_output_options", lambda sd: {"device": 7})
    class Output:
        def __init__(self, **options):
            attempts.append(options)
            if failure == "open":
                cancellation.set()
                raise sd.PortAudioError("Failed while cancelled")
        def start(self):
            cancellation.set()
            raise sd.PortAudioError("Failed while cancelled")
        def close(self):
            pass
    monkeypatch.setattr(sd, "OutputStream", Output)
    player = playback.PlaybackSession()
    with pytest.raises(OperationCancelled):
        player.play(np.full((240, 1), 1000, dtype="int16"), 24000, cancellation, None)
    player.close(abort=True)
    assert len(attempts) == 1 and player.stream is None


@pytest.mark.parametrize("bits,floating,dtype,scale", [(16, False, "<i2", 32768), (32, False, "<i4", 2147483648), (32, True, "<f4", 1)])
def test_loopback_onset_decoder_finds_frame_and_channel(bits, floating, dtype, scale):
    from benchmarks.wasapi_loopback import first_signal
    values = np.zeros((16, 2), dtype=dtype)
    values[7, 1] = -.5 * scale
    assert first_signal(values.tobytes(), 2, bits, floating) == 7
    assert first_signal(bytes(values.nbytes), 2, bits, floating) is None


def test_loopback_probe_is_opt_in_and_rejects_noisy_output_before_arming(monkeypatch):
    from benchmarks import wasapi_loopback as loopback
    monkeypatch.setattr(loopback.C, "OleDLL", lambda *args: pytest.fail("Recording initialized without explicit entry"), raising=False)
    importlib.reload(loopback)
    probe = loopback.LoopbackProbe()
    assert not probe.thread.is_alive()
    probe.last_signal = loopback.time.perf_counter_ns()
    with pytest.raises(RuntimeError, match="not quiet"):
        probe.arm()
    monkeypatch.setattr(loopback, "os", SimpleNamespace(name="posix"))
    with pytest.raises(RuntimeError, match="Windows-only"):
        probe.__enter__()


def test_fixed_seed_paired_confidence_interval_and_reference_word_errors():
    from benchmark_playback import paired_interval
    from benchmark_recognition import edit_distance, words
    assert paired_interval([20] * 20, [10] * 20)["bootstrap_95_percent_interval_ms"] == [10, 10]
    assert edit_distance(words("All the words"), words("all words")) == 1
    assert edit_distance([], words("invented speech")) == 2


def test_invalid_loopback_clock_rejects_a_sample_instead_of_reporting_later_audio():
    from benchmarks.wasapi_loopback import LoopbackProbe
    probe = LoopbackProbe()
    probe.arm()
    probe._sample_error = "Loopback timestamp invalid; sample rejected"
    probe.found.set()
    with pytest.raises(RuntimeError, match="timestamp invalid"):
        probe.result(0)
    probe.arm()
    assert probe.result(0) is None

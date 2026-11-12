"""PCM/queue regressions plus optional actual-engine evidence, no audio devices."""
import importlib.util
import wave
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import numpy as np
import pytest

import config
from core.cancellation import OperationCancelled
from core.interaction import InteractionState
from core.streaming import StreamingReply
from voice import tts
from voice import kokoro_worker
from voice.kokoro_session import session
from voice.kokoro_worker import CONVERSATION_LEXICON
from voice.pronunciation import prepare_kokoro_text
from voice.verify_speech import MixCapture, check_spans, fixtures, read_pcm, replay, save_pcm
from tests.test_kokoro_session import assets, fake_worker


@pytest.fixture
def output_device(monkeypatch):
    import sounddevice as sd
    outputs = []
    class Output:
        def __init__(self, **options):
            self.callback = options["callback"]
            self.stop_event = Event()
            self.active = False
            self.thread = None
        def pump(self):
            pcm = np.empty((13, 1), dtype="int16")
            self.callback(pcm, 13, SimpleNamespace(outputBufferDacTime=1, currentTime=1), None)
            outputs.append(pcm.copy())
        def start(self):
            self.active = True
            self.pump()  # Exercise callbacks running synchronously inside start.
            def run():
                while not self.stop_event.wait(.001):
                    self.pump()
            self.thread = Thread(target=run)
            self.thread.start()
        def stop(self):
            self.active = False
            self.stop_event.set()
            if self.thread:
                self.thread.join(1)
        abort = stop
        def close(self):
            self.stop()
    monkeypatch.setattr(sd, "OutputStream", Output)
    monkeypatch.setattr("voice.playback._output_options", lambda sd: {})
    return outputs


@pytest.mark.parametrize("text", ["What's on your mind?", "I'm here.", "You're here.", "Don't go.", "Can't go.", "It's here."])
def test_contractions_keep_complete_display_and_native_text(text):
    assert tts.clean_for_speech(text) == text
    assert prepare_kokoro_text(text) == text  # Lexicon fix, no global expansion.


def test_conversation_dictionary_uses_installed_symbols_and_preserves_other_contractions():
    path = config.KOKORO_VOICE_DIR / "tokens.txt"
    if not path.is_file():
        pytest.skip("Optional voice pack unavailable")
    tokens = {line.rsplit(" ", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()}
    entries = [line.split() for line in CONVERSATION_LEXICON.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {entry[0] for entry in entries} == {"what's", "wanted"}
    assert all(token in tokens for entry in entries for token in entry[1:])


def test_native_chunk_output_matches_total_frames_without_duplicating_the_final_audio(tmp_path):
    values = [np.linspace(-.3, .3, 19, dtype="float32"), np.linspace(.2, -.2, 23, dtype="float32")]
    def generate(text, callback, **options):
        assert text == "What's on your mind?"
        for index, pcm in enumerate(values):
            callback(pcm, (index+1)/len(values))
        return SimpleNamespace(samples=np.concatenate(values), sample_rate=24000)
    engine = SimpleNamespace(generate=generate, num_speakers=54, sample_rate=24000)
    chunks = []
    result = kokoro_worker.synthesize(engine, {"text": "What's on your mind?", "output": tmp_path/"reply.wav"}, chunks.append)
    assert result["frames"] == sum(message["frames"] for message in chunks) == 42
    assert [Path(message["chunk"]).name for message in chunks] == ["reply-0.wav", "reply-1.wav"]
    pcm = np.concatenate([read_pcm(message["chunk"])[0][:, 0] for message in chunks])
    assert np.array_equal(pcm, (np.concatenate(values)*32767).astype("<i2"))
    assert not (tmp_path/"reply.wav").exists()  # No duplicate whole-WAV fallback.


def test_conversation_lexicon_changes_reload_the_existing_worker(assets, fake_worker, tmp_path, monkeypatch):
    from voice.kokoro_session import KokoroSession
    dictionary = tmp_path/"conversation.lexicon"
    dictionary.write_text("initial")
    monkeypatch.setattr("voice.kokoro_session.CONVERSATION_LEXICON", dictionary)
    worker = KokoroSession()
    try:
        worker.request(assets)
        worker.request(assets)
        assert len(fake_worker[0]) == 1
        dictionary.write_text("changed pronunciation")
        worker.request(assets)
        assert len(fake_worker[0]) == 2
    finally:
        worker.shutdown()


def test_prefetch_keeps_first_last_and_every_pcm_range_across_consecutive_replies(tmp_path, monkeypatch, output_device):
    expected = [np.arange(1000, 1031, dtype="int16").reshape(-1, 1),
                np.arange(-2037, -2000, dtype="int16").reshape(-1, 1)]
    def native_request(directory, cancellation=None, *args, **request):
        assert request["text"] == "What's on your mind?"
        for index, pcm in enumerate(expected):
            path = Path(request["output"]).with_name(f"reply-{index}.wav")
            save_pcm(path, pcm, 24000)
            request["on_chunk"]({"chunk": str(path)})
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    monkeypatch.setattr(session, "request", native_request)
    for trial in range(2):
        result, private = replay("What's on your mind?", "conversation", tmp_path/str(trial), audio=True)
        generated, _ = read_pcm(tmp_path/str(trial)/"generated.wav")
        assert np.array_equal(generated, np.concatenate(expected))
        assert result["chunks"] == 2 and result["callback_pcm_exact"] is True
        assert private["display"] == "What's on your mind?"
    assert output_device[0][0, 0] == expected[0][0, 0]


def test_cancellation_drops_queued_chunks_without_replaying_an_old_response():
    stop = Event()
    played, closed = [], []
    def prepare(text, ready):
        for number in range(3):
            ready(SimpleNamespace(number=number))
    def speak(chunk):
        played.append(chunk.number)
        stop.set()
    reply = StreamingReply(stop, InteractionState(), "Explain", lambda text: None,
                           speak=speak, prepare_stream=prepare, discard=lambda item: closed.append(item.number))
    try:
        reply.feed("What's on your mind?")
        with pytest.raises(OperationCancelled):
            reply.finish()
    finally:
        reply.abort()
    assert played == [0] and sorted(closed) == [0, 1, 2]


def test_pcm_audit_detects_a_lost_first_range_and_a_missing_final_range():
    job = SimpleNamespace(samples=np.arange(20, dtype="int16").reshape(-1, 1))
    with pytest.raises(AssertionError, match="skipped/repeated"):
        check_spans([(job, 1, 20, job.samples[1:])])
    with pytest.raises(AssertionError, match="final"):
        check_spans([(job, 0, 19, job.samples[:19])])


def test_explicit_loopback_save_is_bounded_and_preserves_initial_and_final_pcm(tmp_path):
    capture = MixCapture()
    fmt = {"rate": 24000, "channels": 1, "bits": 16, "floating": False}
    samples = np.arange(100, 200, dtype="int16").reshape(-1, 1)
    capture(samples.tobytes(), 100, 10, 0, fmt)
    assert capture.frames == 0  # No capture before explicit arming.
    capture.arm(20)
    capture(samples.tobytes(), 100, 10, 0, fmt)
    assert capture.frames == 0
    capture(samples.tobytes(), 100, 30, 0, fmt)
    assert capture.frames == 100
    path = tmp_path/"loopback.wav"
    capture.save(path)
    actual, rate = read_pcm(path)
    assert rate == 24000 and np.array_equal(actual, samples)


@pytest.mark.parametrize("fault", ["bound", "discontinuity", "timestamp"])
def test_incomplete_diagnostic_capture_is_rejected_instead_of_misreporting_lost_words(tmp_path, fault):
    capture = MixCapture()
    capture.arm(10)
    fmt = {"rate": 24000, "channels": 1, "bits": 16, "floating": False}
    capture(bytes(200), 24000*16 if fault == "bound" else 100, 30,
            1 if fault == "discontinuity" else 4 if fault == "timestamp" else 0, fmt)
    with pytest.raises(RuntimeError, match="Incomplete diagnostic"):
        capture.save(tmp_path/"invalid.wav")
    assert not (tmp_path/"invalid.wav").exists()


def test_loopback_noise_threshold_is_explicit_and_does_not_change_samples():
    from benchmarks.wasapi_loopback import LoopbackProbe, first_signal
    noise = np.array([.0012, -.002, .03], dtype="<f4").tobytes()
    probe = LoopbackProbe(threshold=.005)
    assert first_signal(noise, 1, 32, True, probe.threshold) == 2
    np.testing.assert_allclose(np.frombuffer(noise, dtype="<f4"), [.0012, -.002, .03], rtol=1e-6)
    assert LoopbackProbe().threshold == .001
    for value in (0, float("nan"), float("inf"), 1):
        with pytest.raises(ValueError):
            LoopbackProbe(threshold=value)


@pytest.mark.skipif(importlib.util.find_spec("sherpa_onnx") is None or not (config.KOKORO_VOICE_DIR/"model.onnx").exists(),
                    reason="Optional Kokoro runtime/voice pack unavailable")
def test_actual_kokoro_keeps_previously_missing_words_in_full_native_and_prefetched_audio(tmp_path):
    # Audio plus native diagnostics catch the former omission. Human listening
    # and saved digital captures remain separate from this regression assertion.
    try:
        process = None
        for mode in ("full", "native", "conversation"):
            for index, text in enumerate(("What's on your mind?", "I wanted to ask you something.", fixtures()[16])):
                result, _ = replay(text, mode, tmp_path/f"{mode}-{index}")
                assert result["oov_messages"] == 0
                assert result["frames"] / result["rate"] > (1 if index == 0 else 1.5)
                assert result["chunks"] >= 1
                if index == 2 and mode != "full":
                    assert result["chunks"] >= 2  # Real native generation/prefetch ordering.
                if process is not None:
                    assert session.process is process
                process = session.process
    finally:
        session.shutdown()

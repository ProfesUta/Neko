import sys
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice

from core.cancellation import OperationCancelled
from voice import voice


def recording_stubs(monkeypatch, start=20, end=24):
    calls = []
    class Input:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return np.full((size, 1), len(calls) + 1, dtype=np.float32), False
    class Detector:
        def reset_states(self):
            pass
        def __call__(self, audio, **kwargs):
            calls.append(int(audio[0]))
            if len(calls) == start:
                return {"start": 0}
            if len(calls) == end:
                return {"end": 1}
    monkeypatch.setattr(voice, "load_models", lambda event=None: None)
    monkeypatch.setattr(voice, "vad_iterator", Detector())
    monkeypatch.setattr(sounddevice, "InputStream", lambda **kwargs: Input())
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(from_numpy=lambda value: value))
    return calls


def test_audio_chunks_processed_once_with_bounded_pre_roll(monkeypatch):
    calls = recording_stubs(monkeypatch)
    detected = []
    audio = voice.record_until_silence(on_speech_start=lambda: detected.append(True))
    assert calls == list(range(1, 25))
    assert detected == [True]
    # 10 chunks of pre-roll, followed by the four remaining speech chunks.
    assert len(audio) == 14 * voice.VAD_CHUNK_SIZE
    assert audio[0] == 11
    assert audio[-1] == 24


def test_stop_during_speech_exits_recording(monkeypatch):
    calls = recording_stubs(monkeypatch, start=1, end=100)
    stop = Event()
    with pytest.raises(OperationCancelled):
        voice.record_until_silence(stop, on_speech_start=stop.set)
    assert len(calls) == 1


def test_continuous_speech_has_a_recording_limit(monkeypatch):
    calls = recording_stubs(monkeypatch, start=1, end=100)
    monkeypatch.setattr(voice, "MAX_RECORD_SECONDS", 2)
    clock = iter(range(1000))
    monkeypatch.setattr(voice.time, "monotonic", lambda: next(clock))
    audio = voice.record_until_silence()
    assert len(calls) < 5
    assert len(audio) > 0


def test_transcription_stop_does_not_return_partial_text(monkeypatch):
    stop = Event()
    def segments():
        yield SimpleNamespace(text="first")
        stop.set()
        yield SimpleNamespace(text="second")
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (segments(), None))
    monkeypatch.setattr(voice, "whisper_model", model)
    with pytest.raises(OperationCancelled):
        voice.transcribe_audio(np.zeros(512), stop)


def test_urgent_tip_interrupts_idle_listening_without_stopping_voice_mode(monkeypatch):
    calls = recording_stubs(monkeypatch, start=100, end=200)
    interrupt = Event()
    interrupt.set()
    assert voice.record_until_silence(idle_interrupt_event=interrupt) is None
    assert calls == [1]


def test_tip_does_not_interrupt_speech_already_in_progress(monkeypatch):
    calls = recording_stubs(monkeypatch, start=1, end=4)
    interrupt = Event()
    audio = voice.record_until_silence(on_speech_start=interrupt.set, idle_interrupt_event=interrupt)
    assert calls == [1, 2, 3, 4]
    assert len(audio) == 4 * voice.VAD_CHUNK_SIZE

import importlib.util
import subprocess
import sys
import time
import wave
from threading import Event, Timer

import numpy as np
import pytest
import sounddevice

from core.cancellation import OperationCancelled
from voice import tts


def test_speech_cleaning_preserves_meaning():
    result = tts.clean_for_speech("**Caf\u00e9** costs $5 (including tax). Use 20% less. `hello` *sighs*")
    assert "Caf\u00e9" in result
    assert "$5 (including tax)" in result
    assert "20%" in result
    assert "hello" in result
    assert "sighs" not in result
    assert "secret_code()" not in tts.clean_for_speech("```py\nsecret_code()\n```")


def test_stop_terminates_real_synthesis_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(tts.importlib.util, "find_spec", lambda name: object())
    real_popen = subprocess.Popen
    processes = []
    def sleeper(*args, **kwargs):
        process = real_popen(
            [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"],
            **kwargs,
        )
        processes.append(process)
        return process
    monkeypatch.setattr(tts.subprocess, "Popen", sleeper)
    stop = Event()
    timer = Timer(0.15, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(OperationCancelled):
            tts._synthesize("A long reply", tmp_path / "test.wav", stop)
    finally:
        timer.cancel()
        timer.join()
    assert processes[0].poll() is not None
    assert time.monotonic() - started < 3


def test_stop_halts_playback(monkeypatch, tmp_path):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 22050, 100, "NONE", "not compressed"))
        output.writeframes(np.zeros(100, dtype="<i2").tobytes())
    stop = Event()
    stopped = []
    monkeypatch.setattr(sounddevice, "play", lambda *args, **kwargs: stop.set())
    monkeypatch.setattr(sounddevice, "get_stream", lambda: type("Stream", (), {"active": True})())
    monkeypatch.setattr(sounddevice, "stop", lambda: stopped.append(True))
    with pytest.raises(OperationCancelled):
        tts._play_wav(path, stop)
    assert stopped == [True]


@pytest.mark.skipif(importlib.util.find_spec("piper") is None, reason="Optional Piper package not installed")
def test_bundled_voice_generates_valid_audio_without_playback(tmp_path):
    path = tmp_path / "synthesis.wav"
    tts._synthesize("Capri is ready.", path, Event())
    with wave.open(str(path), "rb") as audio:
        assert audio.getframerate() == 22050
        assert audio.getnframes() > 1000
        assert audio.getsampwidth() == 2


def test_expired_advice_never_starts_audio(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(tts.time, "monotonic", lambda: clock[0])
    def synthesize(*args):
        clock[0] = 103.0
    monkeypatch.setattr(tts, "_synthesize", synthesize)
    played = []
    monkeypatch.setattr(tts, "_play_wav", lambda *args: played.append(True))
    with pytest.raises(OperationCancelled):
        tts.speak("Take cover.", Event(), start_deadline=102.0)
    assert played == []


def test_fresh_tip_finishes_sentence_after_start_deadline_passes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(tts.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tts, "_synthesize", lambda *args: None)
    stop = Event()
    played = []
    def play(path, cancellation):
        clock[0] = 105.0
        assert not cancellation.is_set()
        played.append(True)
    monkeypatch.setattr(tts, "_play_wav", play)
    tts.speak("Take cover and heal.", stop, start_deadline=102.0)
    assert played == [True]
def test_before_playback_confirms_pose_and_replaces_obsolete_spatial_claim(monkeypatch):
    from voice import tts
    order = []
    monkeypatch.setattr(tts, "_synthesize", lambda text, path, cancellation: order.append(("synthesize", text)))
    monkeypatch.setattr(tts, "_play_wav", lambda *args: order.append(("play", None)))
    def confirm():
        order.append(("confirm pose", None))
        return "The target moved. I can't point at it reliably."
    tts.speak("Right there!", before_playback=confirm)
    assert [item[0] for item in order] == ["synthesize", "confirm pose", "synthesize", "play"]
    assert order[2][1] == "The target moved. I can't point at it reliably."



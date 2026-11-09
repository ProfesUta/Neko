import importlib.util
import wave
from threading import Event

import pytest

import config
from voice import kokoro_session, tts


@pytest.fixture(autouse=True)
def voice_settings(monkeypatch):
    monkeypatch.setattr(config, "TTS_STYLE_ENABLED", True)
    monkeypatch.setattr(config, "KOKORO_SPEED", 1.1)


def test_styles_change_pacing_modestly_and_can_be_disabled(monkeypatch):
    base = tts.kokoro_settings()
    assert tts.kokoro_settings("warm")["speed"] < base["speed"]
    assert tts.kokoro_settings("urgent")["speed"] > base["speed"]
    assert tts.kokoro_settings("playful")["speed"] == pytest.approx(base["speed"] / .98)
    assert tts.kokoro_settings("happy") == tts.kokoro_settings("playful")
    assert tts.kokoro_settings("focused") == base
    assert tts.kokoro_settings({"untrusted": "style"}) == base
    monkeypatch.setattr(config, "TTS_STYLE_ENABLED", False)
    assert tts.kokoro_settings("urgent") == base


@pytest.mark.parametrize("value,expected", [(None, 1.1), ("bad", 1.1),
    (float("nan"), 1.1), (float("inf"), 1.1), (-20, .5), (50, 2.0)])
def test_invalid_pacing_is_sanitized_before_native_synthesis(monkeypatch, value, expected):
    monkeypatch.setattr(config, "KOKORO_SPEED", value)
    assert tts.kokoro_settings()["speed"] == expected


def test_synthesis_passes_style_without_changing_cancellation(monkeypatch, tmp_path):
    calls, token = [], Event()
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    monkeypatch.setattr(kokoro_session.session, "request", lambda *a, **kw: calls.append((a, kw)))
    tts._synthesize("That was close.", tmp_path / "reply.wav", token, style="urgent")
    assert calls[0][0][1] is token
    assert calls[0][1]["speed"] == tts.kokoro_settings("urgent")["speed"]
    assert calls[0][1]["speaker"] == 53


def test_speak_preserves_style_when_replacing_a_stale_spatial_response(monkeypatch):
    calls = []
    monkeypatch.setattr(tts, "_synthesize", lambda *a, **kw: calls.append((a[0], kw)))
    monkeypatch.setattr(tts, "_play_wav", lambda *a: None)
    tts.speak("Right there.", style="focused", before_playback=lambda: "The window moved.")
    assert calls == [("Right there.", {"style": "focused"}), ("The window moved.", {"style": "focused"})]
    calls.clear()
    tts.speak("Hello.")
    assert calls == [("Hello.", {})]


@pytest.mark.skipif(importlib.util.find_spec("sherpa_onnx") is None or not (config.KOKORO_VOICE_DIR / "model.onnx").exists(),
                    reason="Optional Kokoro runtime or voice pack not installed")
def test_real_warm_and_urgent_audio_share_one_resident_voice(tmp_path, monkeypatch):
    session = kokoro_session.KokoroSession()
    monkeypatch.setattr(kokoro_session, "session", session)
    lengths = []
    try:
        process = None
        for style in ("warm", "urgent"):
            output = tmp_path / f"{style}.wav"
            tts._synthesize("That was close. Take a breath and look for cover.", output, Event(), style=style)
            if process is None:
                process = session.process
            assert session.process is process
            with wave.open(str(output)) as audio:
                assert audio.getsampwidth() == 2 and audio.getframerate() == 24000
                lengths.append(audio.getnframes())
        assert lengths[0] > lengths[1] > 1000
    finally:
        session.shutdown()

from threading import Event

import pytest

import config
from voice import tts
from voice import kokoro_session


@pytest.fixture(autouse=True)
def kokoro(monkeypatch):
    monkeypatch.setattr(config, "TTS_BACKEND", "kokoro")
    monkeypatch.setattr(config, "TTS_PERSISTENT_ENABLED", True)


def test_default_creator_preset_is_routed(monkeypatch, tmp_path):
    calls, token = [], Event()
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    monkeypatch.setattr(kokoro_session.session, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
    tts._synthesize("I'm right here.", tmp_path / "capri.wav", token)
    assert calls[0][0][0] == config.KOKORO_VOICE_DIR and calls[0][0][1] is token
    assert calls[0][1]["speaker"] == 53 and calls[0][1]["speed"] == pytest.approx(1.1)
    assert calls[0][1]["text"] == "I'm right here."


def test_warmup_loads_creator_voice_without_recording_or_generating_speech(monkeypatch):
    calls = []
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    monkeypatch.setattr(kokoro_session.session, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
    tts.warm_up(Event())
    assert len(calls) == 1 and "text" not in calls[0][1]
    assert calls[0][1] == {"speaker": 53, "speed": 1.1}


def test_tone_changes_pacing_around_creator_speed(monkeypatch):
    assert tts.kokoro_settings("warm")["speed"] < 1.1 < tts.kokoro_settings("urgent")["speed"]
    monkeypatch.setattr(config, "TTS_STYLE_ENABLED", False)
    assert tts.kokoro_settings("urgent")["speed"] == pytest.approx(1.1)


def test_nonpersistent_mode_closes_model_after_each_utterance(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TTS_PERSISTENT_ENABLED", False)
    calls = []
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    monkeypatch.setattr(kokoro_session.session, "request", lambda *args, **kwargs: calls.append("generate"))
    monkeypatch.setattr(kokoro_session.session, "shutdown", lambda: calls.append("close"))
    tts._synthesize("Hello.", tmp_path / "speech.wav", Event())
    assert calls == ["generate", "close"]


def test_missing_assets_report_install_command(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KOKORO_VOICE_DIR", tmp_path / "missing")
    monkeypatch.setattr(tts.importlib.util, "find_spec", lambda name: object())
    with pytest.raises(RuntimeError, match="python -m voice.install_capri_voice"):
        tts.warm_up(Event())


def test_unknown_backend_does_not_silently_use_another_voice(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TTS_BACKEND", "typo")
    with pytest.raises(RuntimeError, match="Unknown speech backend"):
        tts._synthesize("Hello.", tmp_path / "speech.wav", Event())

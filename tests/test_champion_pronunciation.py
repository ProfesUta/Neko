"""Regression coverage for Kokoro silently dropping unknown champion names."""

import importlib.util
import json
import re
import wave
from pathlib import Path
from threading import Event

import numpy as np
import pytest

import config
from core.context import Context
from core.league_voice import HOVER_REACTIONS, LOCK_REACTIONS
from gui.workers import VoiceWorker
from voice import tts
from voice.kokoro_session import session
from voice.kokoro_worker import CHAMPION_LEXICON
from voice.pronunciation import prepare_kokoro_text


@pytest.mark.parametrize("text,expected", [
    ("Okay, Ahri. Your call.", "Okay, Ahri. Your call."),
    ("Kai'Sa, K'Sante, Cho'Gath, Kha'Zix, Kog'Maw, Rek'Sai, Vel'Koz, Bel'Veth.",
     "KaiSa, KSante, ChoGath, KhaZix, KogMaw, RekSai, VelKoz, BelVeth."),
    ("Kai\u2019Sa's turn, then K\u2019Sante.", "KaiSa's turn, then KSante."),
    ("Jarvan IV and Nunu & Willump.", "Jarvan the Fourth and Nunu and Willump."),
    ("IV drip. I'm here. Rock & roll.", "IV drip. I'm here. Rock & roll."),
])
def test_speech_normalization_keeps_names_and_other_words(text, expected):
    assert prepare_kokoro_text(text) == expected


@pytest.mark.parametrize("template", HOVER_REACTIONS + LOCK_REACTIONS)
def test_every_hover_and_lock_line_names_the_actual_champion(template):
    assert "Yuumi" in template.format(champion="Yuumi")


def test_spoken_name_normalization_reaches_resident_voice_without_changing_display(monkeypatch):
    requests = []
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    monkeypatch.setattr(session, "request", lambda *args, **kwargs: requests.append(kwargs))
    displayed = "Okay... Kai'Sa. Your call."
    with tts.prepare_speech(displayed, Event()) as speech:
        assert speech.text == displayed  # Chat/transcript text keeps the official spelling.
    assert requests[0]["text"] == "Okay... KaiSa. Your call."
    assert requests[0]["speaker"] == config.KOKORO_SPEAKER_ID


def test_dictionary_phonemes_and_all_cached_champion_names_are_supported():
    tokens_path = config.KOKORO_VOICE_DIR / "tokens.txt"
    cache_path = Path(__file__).resolve().parents[1] / "data" / "league-champions.json"
    if not tokens_path.exists() or not cache_path.exists():
        pytest.skip("Optional voice pack or local champion cache is not installed")
    tokens = {line.rsplit(" ", 1)[0] for line in tokens_path.read_text(encoding="utf-8").splitlines()}
    entries = [line.split() for line in CHAMPION_LEXICON.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len({entry[0] for entry in entries}) == len(entries), "Duplicate pronunciation overrides"
    assert all(len(entry) > 1 and all(token in tokens for token in entry[1:]) for entry in entries)
    base = config.KOKORO_VOICE_DIR / "lexicon-us-en.txt"
    known = {line.split()[0].lower() for line in base.read_text(encoding="utf-8").splitlines() if line.strip()}
    known.update(entry[0] for entry in entries)
    names = json.loads(cache_path.read_text(encoding="utf-8"))["names"].values()
    missing = [(name, word) for name in names
               for word in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)*", prepare_kokoro_text(name).lower())
               if word not in known]
    assert missing == []


@pytest.mark.skipif(importlib.util.find_spec("sherpa_onnx") is None
                    or not (config.KOKORO_VOICE_DIR / "model.onnx").exists(),
                    reason="Optional Kokoro runtime or voice pack is not installed")
def test_formerly_silent_names_generate_audible_audio_through_actual_speech_pipeline(monkeypatch):
    # Inspect generated WAVs instead of playing audio or opening microphone input.
    # Testing names alone catches skipped words even when the surrounding sentence speaks.
    checked = []
    def inspect_audio(path, cancel_event, on_playback_start=None):
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(float) / 32768
        assert duration > .3  # Previously Ahri and Yuumi produced .192 s of silence.
        assert np.sqrt(np.mean(samples ** 2)) > .005
        checked.append(True)
        if on_playback_start:
            on_playback_start()
    monkeypatch.setattr(tts, "_play_wav", inspect_audio)
    try:
        for champion in ("Ahri", "Yuumi", "Kai'Sa", "K'Sante", "Jarvan IV", "Nunu & Willump"):
            tts.speak(champion + ".", Event())
        assert len(checked) == 6
    finally:
        session.shutdown()


def test_finished_pointing_turn_cannot_replace_subsequent_champion_speech(monkeypatch):
    from types import SimpleNamespace
    from core.cancellation import OperationCancelled

    finished, spoken = [], []
    context = Context()
    context.spatial = SimpleNamespace(
        finish_turn=finished.append,
        prepare_response=lambda *args: pytest.fail("Completed pointing state replaced champion speech"),
    )
    worker = VoiceWorker([], context)
    worker._spatial_generation = 7
    worker._question = "Where is Discord?"
    worker._finish_spatial()
    assert finished == [7] and worker._spatial_generation is None and worker._question == ""
    def speak(text, **kwargs):
        kwargs["before_playback"]()
        spoken.append(text)
    monkeypatch.setattr(tts, "speak", speak)
    line = LOCK_REACTIONS[0].format(champion="Ahri")
    assert worker._prepare_spatial_response(line) == line
    worker._speak(line, playback_guard=lambda: True)
    assert spoken == [line]
    with pytest.raises(OperationCancelled):
        worker._speak(line, playback_guard=lambda: False)
    assert spoken == [line]  # Stale announcements still respect their playback guard.

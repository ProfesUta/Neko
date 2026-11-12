import json
import importlib.util
import wave
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

import config
from core.interaction import InteractionState
from core.streaming import StreamingReply
from voice import kokoro_worker, tts
from voice.kokoro_session import KokoroSession
from voice.personal_pronunciation import compile_profile, read_profile, PersonalTts
from voice.pronunciation import prepare_kokoro_text
from tests.test_kokoro_session import assets, fake_worker


@pytest.fixture
def dictionaries(assets, tmp_path):
    symbols = ["ɹ", "ˈ", "ɑ", "b", "ɪ", "n", "s", "z", "h", "ə", "l", "o", "ʊ"]
    (assets / "tokens.txt").write_text("\n".join(f"{value} {index}" for index, value in enumerate(symbols)), encoding="utf-8")
    (assets / "lexicon-us-en.txt").write_text("robin ɹ ˈ ɑ b ɪ n\nhello h ə l ˈ o ʊ\n", encoding="utf-8")
    champions = tmp_path / "champions.lexicon"
    champions.write_text("ahri ˈ ɑ ɹ ɪ\n", encoding="utf-8")
    return assets, champions


def profile_file(tmp_path, **entry):
    path = tmp_path / "private.json"
    path.write_text(json.dumps({"entries": [{"display_name": "DemoPerson", "spoken_pronunciation": "robin", **entry}]}), encoding="utf-8")
    return path


@pytest.mark.parametrize("text,template", [
    ("DemoPerson, I am here.", "{alias}, I am here."),
    ("Hello, DemoPerson, I am here.", "Hello, {alias}, I am here."),
    ("I am here, DemoPerson!", "I am here, {alias}!"),
    ('Hello, "DemoPerson"!', "Hello, {alias}!"),
    ("Hello, ‘DemoPerson’!", "Hello, {alias}!"),
    ("(demoperson).", "{alias}."),
    ("DEMOPERSON's turn.", "{alias}'s turn."),
])
def test_complete_spoken_name_matches_punctuation_and_positions(dictionaries, tmp_path, text, template):
    directory, champions = dictionaries
    profile = compile_profile(profile_file(tmp_path), directory, champions)
    alias = profile.lexicon.split()[0]
    assert profile.apply(text) == template.format(alias=alias)
    assert "demoperson" not in profile.lexicon  # No private display names in derivative files.


def test_substrings_unrelated_names_and_champion_dictionary_are_unchanged(dictionaries, tmp_path):
    directory, champions = dictionaries
    original = champions.read_bytes()
    profile = compile_profile(profile_file(tmp_path), directory, champions)
    text = "DemoPersons DemoPerson123 superDemoPerson DemoPerson_other DemoPerson'll Ahri."
    assert profile.apply(text) == text
    assert champions.read_bytes() == original
    with pytest.raises(ValueError, match="protected champion"):
        compile_profile(profile_file(tmp_path, display_name="Ahri"), directory, champions)


def test_explicit_phonemes_take_precedence_and_invalid_values_are_not_echoed(dictionaries, tmp_path):
    directory, champions = dictionaries
    path = profile_file(tmp_path, spoken_pronunciation="PrivateUnsupportedWord", phonemes="ɹ ˈ ɑ b ɪ n")
    assert compile_profile(path, directory, champions).lexicon.splitlines()[0].endswith("ɹ ˈ ɑ b ɪ n")
    for entry in ({"phonemes": ["PrivateUnsupportedToken"]}, {"spoken_pronunciation": "PrivateUnsupportedWord"}):
        with pytest.raises(ValueError) as error:
            compile_profile(profile_file(tmp_path, **entry), directory, champions)
        assert "PrivateUnsupported" not in str(error.value) and "DemoPerson" not in str(error.value)


@pytest.mark.parametrize("payload", [{"entries": "PrivateName"}, {"entries": [42]}, {"entries": [{"display_name": "bad\nline"}]}])
def test_malformed_private_configuration_fails_without_dumping_contents(tmp_path, payload):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError) as error:
        read_profile(path)
    assert "PrivateName" not in str(error.value) and "bad\nline" not in str(error.value)


def test_disabled_profile_needs_no_override_and_duplicate_names_are_rejected(dictionaries, tmp_path):
    directory, champions = dictionaries
    assert not compile_profile(profile_file(tmp_path, enabled=False, spoken_pronunciation=""), directory, champions).replacements
    path = profile_file(tmp_path)
    entry = json.loads(path.read_text())["entries"][0]
    path.write_text(json.dumps({"entries": [entry, {**entry, "display_name": "DEMOPERSON"}]}))
    with pytest.raises(ValueError, match="duplicate"):
        read_profile(path)


def test_multiword_names_match_longest_without_changing_other_words(dictionaries, tmp_path):
    directory, champions = dictionaries
    path = profile_file(tmp_path)
    entries = json.loads(path.read_text())["entries"]
    entries.append({**entries[0], "display_name": "DemoPerson Example"})
    path.write_text(json.dumps({"entries": entries}))
    profile = compile_profile(path, directory, champions)
    longer_alias = profile.lexicon.splitlines()[2].split()[0]
    assert profile.apply("DemoPerson Example!") == longer_alias + "!"


def test_private_lexicon_is_ephemeral_and_same_native_voice_is_wrapped(dictionaries, tmp_path, monkeypatch):
    directory, champions = dictionaries
    monkeypatch.setattr(kokoro_worker, "CHAMPION_LEXICON", champions)
    loaded, generated = [], []
    engine = SimpleNamespace(num_speakers=54, sample_rate=24000,
        generate=lambda text, **options: generated.append((text, options)))
    def native(assets, threads, cpu, extra):
        assert extra.is_file()
        loaded.append(extra)
        return engine
    monkeypatch.setattr(kokoro_worker, "_create_native_tts", native)
    wrapped = kokoro_worker.create_tts(directory, 8, None, profile_file(tmp_path))
    assert wrapped.engine is engine and wrapped.sample_rate == 24000
    assert not loaded[0].exists()
    callback = lambda samples, progress: None
    wrapped.generate("Hello, DemoPerson!", sid=53, speed=1.1, callback=callback)
    assert "DemoPerson" not in generated[0][0]
    assert generated[0][1] == {"sid": 53, "speed": 1.1, "callback": callback}


def test_native_streaming_pcm_and_final_chunk_marker_are_preserved(dictionaries, tmp_path):
    directory, champions = dictionaries
    received = []
    samples = np.array([-.2, 0, .2], dtype=np.float32)
    def generate(text, callback, **options):
        received.append(text)
        callback(samples, 1)
        return SimpleNamespace(samples=samples, sample_rate=24000)
    engine = SimpleNamespace(num_speakers=54, sample_rate=24000, generate=generate)
    wrapped = PersonalTts(engine, compile_profile(profile_file(tmp_path), directory, champions))
    chunks = []
    kokoro_worker.synthesize(wrapped, {"text": "DemoPerson.", "output": tmp_path / "reply.wav"}, chunks.append)
    assert chunks[0]["generation_complete"] is True and chunks[0]["frames"] == 3
    with wave.open(chunks[0]["chunk"], "rb") as source:
        assert np.frombuffer(source.readframes(3), dtype="<i2").tolist() == [-6553, 0, 6553]
    assert "DemoPerson" not in received[0]


def test_display_cleaning_and_llm_token_boundaries_do_not_split_the_pronunciation(dictionaries, tmp_path):
    directory, champions = dictionaries
    profile = compile_profile(profile_file(tmp_path), directory, champions)
    displayed, native = [], []
    reply = StreamingReply(Event(), InteractionState(), "Explain", displayed.append,
        speak=lambda text: native.append(profile.apply(prepare_kokoro_text(tts.clean_for_speech(text)))))
    try:
        reply.feed("Hello, Demo")
        reply.feed("Person")
        reply.feed(". ")
        reply.finish()
    finally:
        reply.abort()
    assert reply.text == "Hello, DemoPerson. " and displayed[-1] == reply.text
    assert len(native) == 1 and "DemoPerson" not in native[0]


def test_private_file_change_or_removal_reloads_only_when_needed(assets, fake_worker, tmp_path, monkeypatch):
    path = profile_file(tmp_path)
    monkeypatch.setattr(config, "PERSONAL_PRONUNCIATIONS_FILE", path)
    processes, commands = fake_worker
    session = KokoroSession()
    try:
        session.request(assets)
        session.request(assets)
        assert len(processes) == 1 and commands[0][-2:] == ["--personal-pronunciations", str(path.resolve())]
        profile_file(tmp_path, phonemes=["n"])
        session.request(assets)
        assert len(processes) == 2
        path.unlink()
        session.request(assets)
        assert len(processes) == 3
    finally:
        session.shutdown()


@pytest.mark.parametrize("answer", ["n", "y"])
def test_listening_configuration_requires_confirmation_and_keeps_other_entries(dictionaries, tmp_path, monkeypatch, answer, capsys):
    from voice import verify_pronunciation as verify
    directory, champions = dictionaries
    monkeypatch.setattr(config, "KOKORO_VOICE_DIR", directory)
    monkeypatch.setattr(verify, "CHAMPION_LEXICON", champions)
    destination = tmp_path / "private.json"
    other = {"display_name": "OtherExample", "spoken_pronunciation": "", "enabled": False}
    destination.write_text(json.dumps({"entries": [other]}))
    original = destination.read_bytes()
    answers = iter(["DemoPerson", "robin", "", answer])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    played = []
    def compare(path, output, audio=False):
        assert json.loads(path.read_text())["entries"][0]["display_name"] == "DemoPerson"
        played.append(audio)
    monkeypatch.setattr(verify, "compare", compare)
    assert verify.configure(destination, tmp_path / "audio") is (answer == "y")
    assert played == [True]
    if answer == "y":
        saved = json.loads(destination.read_text())["entries"]
        assert saved[0]["display_name"] == "DemoPerson" and saved[1] == other
    else:
        assert destination.read_bytes() == original
    assert "DemoPerson" not in capsys.readouterr().out


@pytest.mark.skipif(importlib.util.find_spec("sherpa_onnx") is None or not (config.KOKORO_VOICE_DIR / "model.onnx").exists(),
                    reason="Optional Kokoro runtime/voice pack unavailable")
def test_actual_resident_worker_speaks_a_private_oov_name_in_full_and_native_streams(tmp_path, monkeypatch):
    path = profile_file(tmp_path, spoken_pronunciation="hello")
    monkeypatch.setattr(config, "PERSONAL_PRONUNCIATIONS_FILE", path)
    session = KokoroSession()
    output = tmp_path / "real.wav"
    try:
        session.request(config.KOKORO_VOICE_DIR)
        process = session.process
        for streaming in (False, True):
            counts = []
            def ready(message):
                counts.append(message["frames"])
                Path(message["chunk"]).unlink()
            session.request(config.KOKORO_VOICE_DIR, text="DemoPerson.", output=output,
                            on_chunk=ready if streaming else None, speaker=53, speed=1.1)
            if not streaming:
                with wave.open(str(output), "rb") as source:
                    counts.append(source.getnframes())
            assert sum(counts) > 24000 * .3  # A skipped OOV previously produced ~.19s.
            assert session.process is process
        assert not any("OOV word skipped" in line for line in session._diagnostics)
    finally:
        session.shutdown()

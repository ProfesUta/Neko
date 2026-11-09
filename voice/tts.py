"""Local speech synthesis and interruptible audio playback."""

import importlib.util
import math
import re
import tempfile
import time
import wave
from pathlib import Path
from threading import Event

from config import TTS_TIMEOUT_SECONDS
import config
from core.cancellation import AnyEvent, DeadlineEvent, check_cancelled


def kokoro_settings(style=None):
    try:
        speed = float(config.KOKORO_SPEED)
    except (TypeError, ValueError, OverflowError):
        speed = 1.1
    if not math.isfinite(speed):
        speed = 1.1
    multiplier = 1.0
    if config.TTS_STYLE_ENABLED and isinstance(style, str):
        multiplier = {"playful": .98, "happy": .98, "warm": 1.06,
                      "urgent": .94, "focused": 1.0}.get(style, 1.0)
    return {"speaker": config.KOKORO_SPEAKER_ID, "speed": max(.5, min(2.0, speed / multiplier))}


def _require_kokoro():
    if config.TTS_BACKEND != "kokoro":
        raise RuntimeError(f"Unknown speech backend: {config.TTS_BACKEND}. Set CAPRI_TTS_BACKEND=kokoro.")
    if importlib.util.find_spec("sherpa_onnx") is None:
        raise RuntimeError("Capri's local voice runtime is missing. Run: python -m pip install -r requirements.txt")
    from voice.kokoro_worker import validate_assets
    try:
        validate_assets(config.KOKORO_VOICE_DIR)
    except ValueError as error:
        raise RuntimeError("Capri's local voice assets are missing or incomplete. Run: python -m voice.install_capri_voice") from error


def clean_for_speech(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"```.*?```", " Code is shown in chat. ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Preserve emphasized words, numbers, accents, and meaningful punctuation.
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    text = re.sub(r"\*(?:smirks|sighs|laughs|chuckles|shrugs|crosses arms|tilts head)[^*]*\*", "", text, flags=re.I)
    text = re.sub(r"(?<!\w)\*([^\s*](?:[^*\n]*[^\s*])?)\*(?!\w)", r"\1", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)
    text = re.sub(r"<\s*3", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _synthesize(text, output_path, cancel_event, style=None):
    check_cancelled(cancel_event)
    _require_kokoro()
    from voice.kokoro_session import session
    try:
        session.request(config.KOKORO_VOICE_DIR, cancel_event, TTS_TIMEOUT_SECONDS,
                        text=text, output=output_path, **kokoro_settings(style))
    finally:
        if not config.TTS_PERSISTENT_ENABLED:
            session.shutdown()


def warm_up(cancel_event=None):
    """Run at startup or before listening; no audio is generated or played."""
    if config.TTS_PERSISTENT_ENABLED:
        check_cancelled(cancel_event)
        _require_kokoro()
        from voice.kokoro_session import session
        session.request(config.KOKORO_VOICE_DIR, cancel_event, TTS_TIMEOUT_SECONDS, **kokoro_settings())


def _play_wav(path, cancel_event, on_playback_start=None):
    import numpy as np
    import sounddevice as sd

    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise RuntimeError("Speech engine returned an unsupported audio format.")
        data = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        data = data.reshape(-1, audio.getnchannels())
        rate = audio.getframerate()
    check_cancelled(cancel_event)
    try:
        sd.play(data, samplerate=rate)
        if on_playback_start:
            on_playback_start()
        while sd.get_stream().active:
            check_cancelled(cancel_event)
            if cancel_event is not None:
                cancel_event.wait(0.03)
            else:
                time.sleep(0.03)
    finally:
        sd.stop()


class PreparedSpeech:
    """An owned temporary WAV that can be generated ahead of current playback."""

    def __init__(self, directory, text, cancel_event, start_cancellation,
                 on_playback_start, before_playback, settings):
        self._directory = directory
        self.path = Path(directory.name) / "reply.wav"
        self.text = text
        self._cancel_event = cancel_event
        self._start_cancellation = start_cancellation
        self._on_playback_start = on_playback_start
        self._before_playback = before_playback
        self._settings = settings
        self._closed = self._used = False

    def play(self):
        if self._closed or self._used:
            raise RuntimeError("Prepared speech has already been consumed")
        self._used = True
        check_cancelled(self._start_cancellation)
        if self._before_playback is not None:
            # Revalidate the target now, even if this WAV was prefetched earlier.
            replacement = self._before_playback()
            check_cancelled(self._start_cancellation)
            if replacement and clean_for_speech(replacement) != self.text:
                self.text = clean_for_speech(replacement)
                _synthesize(self.text, self.path, self._start_cancellation, **self._settings)
                check_cancelled(self._start_cancellation)
        # A tip's freshness deadline applies to starting, not to finishing it.
        if self._on_playback_start is None:
            _play_wav(self.path, self._cancel_event)
        else:
            _play_wav(self.path, self._cancel_event, self._on_playback_start)

    def close(self):
        if not self._closed:
            self._closed = True
            self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def prepare_speech(text: str, cancel_event=None, start_deadline=None, on_playback_start=None, before_playback=None, style=None):
    """Generate without playing, keeping cancellation and temporary-file ownership."""
    check_cancelled(cancel_event)
    cleaned = clean_for_speech(text)
    if not cleaned:
        return
    start_cancellation = cancel_event
    if start_deadline is not None:
        start_cancellation = AnyEvent(cancel_event or Event(), DeadlineEvent(start_deadline))
    settings = {"style": style} if style is not None else {}
    speech = PreparedSpeech(tempfile.TemporaryDirectory(prefix="capri-speech-"), cleaned,
                            cancel_event, start_cancellation, on_playback_start, before_playback, settings)
    try:
        _synthesize(cleaned, speech.path, start_cancellation, **settings)
        check_cancelled(start_cancellation)
        return speech
    except BaseException:
        speech.close()
        raise


def speak(text: str, cancel_event=None, start_deadline=None, on_playback_start=None, before_playback=None, style=None):
    """Generate then play; streaming callers can prepare the next utterance instead."""
    speech = prepare_speech(text, cancel_event, start_deadline, on_playback_start, before_playback, style)
    if speech is not None:
        with speech:
            speech.play()

"""Piper synthesis and interruptible audio playback."""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from threading import Event

from config import TTS_TIMEOUT_SECONDS
import config
from core.cancellation import AnyEvent, DeadlineEvent, check_cancelled

BASE_DIR = Path(__file__).resolve().parent.parent
VOICE_PATH = BASE_DIR / "assets" / "voices" / "en_US-amy-medium.onnx"


def clean_for_speech(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"```.*?```", " Code is shown in chat. ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Preserve emphasized words, numbers, accents, and meaningful punctuation.
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    text = re.sub(r"\*(?:smirks|sighs|laughs|chuckles|shrugs|crosses arms|tilts head)[^*]*\*", "", text, flags=re.I)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)
    text = re.sub(r"<\s*3", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _synthesize(text, output_path, cancel_event):
    check_cancelled(cancel_event)
    if config.TTS_PERSISTENT_ENABLED:
        _require_piper()
        from voice.piper_session import session
        session.request(VOICE_PATH, cancel_event, TTS_TIMEOUT_SECONDS, text=text, output=output_path)
        return
    _synthesize_once(text, output_path, cancel_event)


def warm_up(cancel_event=None):
    """Run at startup or before listening; no audio is generated or played."""
    if config.TTS_PERSISTENT_ENABLED:
        check_cancelled(cancel_event)
        _require_piper()
        from voice.piper_session import session
        session.request(VOICE_PATH, cancel_event, TTS_TIMEOUT_SECONDS)


def _require_piper():
    if importlib.util.find_spec("piper") is None:
        raise RuntimeError(
            "Piper speech support is not installed. Run: python -m pip install -r requirements.txt"
        )


def _synthesize_once(text, output_path, cancel_event):
    check_cancelled(cancel_event)
    _require_piper()
    process = subprocess.Popen(
        [sys.executable, "-m", "piper", "--model", str(VOICE_PATH),
         "--output_file", str(output_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    pending_input = text
    deadline = time.monotonic() + TTS_TIMEOUT_SECONDS
    try:
        while True:
            check_cancelled(cancel_event)
            if time.monotonic() >= deadline:
                raise RuntimeError("Speech generation took too long. Please try again.")
            try:
                _stdout, stderr = process.communicate(input=pending_input, timeout=0.05)
                if process.returncode:
                    raise RuntimeError(f"Piper failed: {stderr.strip()}")
                return
            except subprocess.TimeoutExpired:
                # communicate() retains the buffered input after its first call.
                pending_input = None
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def _play_wav(path, cancel_event, on_playback_start=None):
    import numpy as np
    import sounddevice as sd

    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise RuntimeError("Piper returned an unsupported audio format.")
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


def speak(text: str, cancel_event=None, start_deadline=None, on_playback_start=None, before_playback=None):
    """Cancel both synthesis and playback without waiting for the whole reply."""
    check_cancelled(cancel_event)
    cleaned = clean_for_speech(text)
    if not cleaned:
        return
    if not VOICE_PATH.is_file():
        raise FileNotFoundError(f"Missing Piper voice model: {VOICE_PATH}")
    with tempfile.TemporaryDirectory(prefix="capri-speech-") as directory:
        output = Path(directory) / "reply.wav"
        start_cancellation = cancel_event
        if start_deadline is not None:
            start_cancellation = AnyEvent(cancel_event or Event(), DeadlineEvent(start_deadline))
        _synthesize(cleaned, output, start_cancellation)
        check_cancelled(start_cancellation)
        if before_playback is not None:
            # Synthesis can take seconds. Reconfirm the pointing action before
            # starting audio, and replace an obsolete 'right there' if necessary.
            replacement = before_playback()
            check_cancelled(start_cancellation)
            if replacement and clean_for_speech(replacement) != cleaned:
                _synthesize(clean_for_speech(replacement), output, start_cancellation)
                check_cancelled(start_cancellation)
        # Once a fresh tip starts, finish the short sentence; Stop still wins.
        if on_playback_start is None:
            _play_wav(output, cancel_event)
        else:
            _play_wav(output, cancel_event, on_playback_start)

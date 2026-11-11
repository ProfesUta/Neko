"""Local speech synthesis and interruptible audio playback."""

import importlib.util
import math
import re
import tempfile
import time
import wave
from pathlib import Path
from threading import Event, Lock

from config import TTS_TIMEOUT_SECONDS
import config
from core.cancellation import AnyEvent, DeadlineEvent, check_cancelled
from voice.pronunciation import prepare_kokoro_text
from core.latency import current_trace, mark, span, trace_scope


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
        with span("tts_synthesis"):
            session.request(config.KOKORO_VOICE_DIR, cancel_event, TTS_TIMEOUT_SECONDS,
                            text=prepare_kokoro_text(text), output=output_path, **kokoro_settings(style))
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
    from voice.playback import play_samples

    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2:
            raise RuntimeError("Speech engine returned an unsupported audio format.")
        data = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        data = data.reshape(-1, audio.getnchannels())
        rate = audio.getframerate()
    check_cancelled(cancel_event)
    play_samples(data, rate, cancel_event, on_playback_start)


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
        self._trace = current_trace()

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
        with trace_scope(self._trace):
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
    mark("tts_submitted")
    start_cancellation = cancel_event
    if start_deadline is not None:
        start_cancellation = AnyEvent(cancel_event or Event(), DeadlineEvent(start_deadline))
    settings = {"style": style} if style is not None else {}
    speech = PreparedSpeech(tempfile.TemporaryDirectory(prefix="capri-speech-"), cleaned,
                            cancel_event, start_cancellation, on_playback_start, before_playback, settings)
    try:
        _synthesize(cleaned, speech.path, start_cancellation, **settings)
        mark("audio_ready")
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


class _AudioChunks:
    """Own one native utterance until generation and every queued chunk finish."""
    def __init__(self, cancellation, on_started, on_finished):
        self.directory = tempfile.TemporaryDirectory(prefix="capri-stream-")
        self.cancellation = cancellation
        self.on_started, self.on_finished = on_started, on_finished
        self.lock = Lock()
        self.refs = 0
        self.done = self.closed = self.started = False
        self.trace = current_trace()

    def started_playing(self):
        if not self.started:
            self.started = True
            if self.on_started:
                self.on_started()

    def release(self, generated=False):
        with self.lock:
            if generated:
                self.done = True
            else:
                self.refs -= 1
            close = self.done and self.refs == 0 and not self.closed
            if close:
                self.closed = True
        if close:
            try:
                if self.started and self.on_finished:
                    self.on_finished()
            finally:
                self.directory.cleanup()


class _AudioChunk:
    def __init__(self, owner, path):
        self.owner, self.path = owner, path
        self.closed = self.used = False

    def play(self):
        if self.closed or self.used:
            raise RuntimeError("Audio chunk was already consumed")
        self.used = True
        check_cancelled(self.owner.cancellation)
        with trace_scope(self.owner.trace):
            _play_wav(self.path, self.owner.cancellation, self.owner.started_playing)

    def close(self):
        if not self.closed:
            self.closed = True
            self.owner.release()


def stream_speech(text, on_ready, cancel_event=None, on_playback_start=None, on_finished=None, style=None):
    """Kokoro's actual native chunks, using the existing synthesis/audio threads.

Does not claim intra-phoneme streaming: callback granularity is chosen by Kokoro.
Spatial/deadline guarded replies retain the complete-WAV validation path.
"""
    from voice.kokoro_session import session
    check_cancelled(cancel_event)
    cleaned = clean_for_speech(text)
    if not cleaned:
        return
    mark("tts_submitted")
    _require_kokoro()
    owner = _AudioChunks(cancel_event, on_playback_start, on_finished)
    output = Path(owner.directory.name) / "reply.wav"
    def ready(message):
        check_cancelled(cancel_event)
        path = Path(message["chunk"]).resolve()
        if path.parent != output.parent.resolve() or not path.is_file():
            raise RuntimeError("Kokoro returned an invalid audio chunk path")
        mark("audio_ready")
        with owner.lock:
            owner.refs += 1
        chunk = _AudioChunk(owner, path)
        try:
            on_ready(chunk)
        except BaseException:
            chunk.close()
            raise
    try:
        with span("tts_stream_generation"):
            session.request(config.KOKORO_VOICE_DIR, cancel_event, TTS_TIMEOUT_SECONDS,
                text=prepare_kokoro_text(cleaned), output=output, on_chunk=ready, **kokoro_settings(style))
    finally:
        owner.release(generated=True)
        if not config.TTS_PERSISTENT_ENABLED:
            session.shutdown()

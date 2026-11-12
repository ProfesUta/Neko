"""Lazy voice models and bounded, cancellable speech capture."""

import os
import logging
import sysconfig
import time
from collections import deque
from pathlib import Path
from threading import Lock

from config import MAX_RECORD_SECONDS, WHISPER_BEAM_SIZE, WHISPER_LANGUAGE, WHISPER_MODEL, WHISPER_DEVICE, VOICE_SILENCE_MS
import config
from core.cancellation import check_cancelled
from core.latency import current_trace, mark, span

SAMPLE_RATE = 16000
VAD_CHUNK_SIZE = 512  # 32 ms at 16 kHz.
SILENCE_DURATION_MS = max(200, min(2000, VOICE_SILENCE_MS))
WAIT_TIMEOUT_SECONDS = 30
PRE_ROLL_CHUNKS = 10
whisper_model = None
vad_iterator = None
_dll_handles = []
_model_load_lock = Lock()


def load_models(cancel_event=None):
    """Share startup/voice loads without ever opening an input device."""
    check_cancelled(cancel_event)
    if whisper_model is not None and vad_iterator is not None:
        return
    while not _model_load_lock.acquire(timeout=0.05):
        check_cancelled(cancel_event)
    try:
        check_cancelled(cancel_event)
        if whisper_model is None or vad_iterator is None:
            _load_models_locked(cancel_event)
    finally:
        _model_load_lock.release()


def _load_models_locked(cancel_event):
    global whisper_model, vad_iterator
    check_cancelled(cancel_event)
    if os.name == "nt" and not _dll_handles:
        packages = Path(sysconfig.get_paths()["purelib"])
        directories = [
            packages / "nvidia" / name / "bin"
            for name in ("cublas", "cudnn", "cuda_nvrtc")
        ]
        for directory in directories:
            if directory.is_dir():
                _dll_handles.append(os.add_dll_directory(str(directory)))

    import torch
    from faster_whisper import WhisperModel
    from silero_vad import VADIterator, load_silero_vad

    if whisper_model is None:
        if WHISPER_DEVICE not in ("auto", "cpu", "cuda"):
            raise ValueError("CAPRI_WHISPER_DEVICE must be auto, cpu or cuda")
        device = WHISPER_DEVICE
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        elif device == "cuda" and not torch.cuda.is_available():
            logging.warning("CUDA recognition is unavailable; using CPU int8")
            device = "cpu"
        thread_options = {}
        if device == "cpu" and config.WHISPER_CPU_THREADS > 0:
            thread_options["cpu_threads"] = min(16, config.WHISPER_CPU_THREADS)
        whisper_model = WhisperModel(
            WHISPER_MODEL, device=device,
            compute_type="float16" if device == "cuda" else "int8",
            **thread_options,
        )
        logging.info("Speech recognition ready on %s", device)
    check_cancelled(cancel_event)
    if vad_iterator is None:
        vad_iterator = VADIterator(
            load_silero_vad(), threshold=0.5, sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=SILENCE_DURATION_MS, speech_pad_ms=200,
        )
    check_cancelled(cancel_event)


def record_until_silence(stop_event=None, on_speech_start=None, idle_interrupt_event=None):
    import numpy as np
    import sounddevice as sd
    import torch

    load_models(stop_event)
    pre_roll = deque(maxlen=PRE_ROLL_CHUNKS)
    recorded_chunks = []
    speech_started_at = None
    started_at = time.monotonic()
    vad_iterator.reset_states()
    trace = current_trace()
    samples_read = 0

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
        blocksize=VAD_CHUNK_SIZE,
    ) as stream:
        while True:
            check_cancelled(stop_event)
            now = time.monotonic()
            if speech_started_at is None and now - started_at >= WAIT_TIMEOUT_SECONDS:
                return None
            if speech_started_at is not None and now - speech_started_at >= MAX_RECORD_SECONDS:
                break

            chunk, _overflowed = stream.read(VAD_CHUNK_SIZE)
            samples_read += VAD_CHUNK_SIZE
            chunk = chunk.copy()
            if speech_started_at is None:
                pre_roll.append(chunk)
            else:
                recorded_chunks.append(chunk)

            # The recurrent model must see each audio chunk exactly once.
            speech = vad_iterator(torch.from_numpy(chunk[:, 0]), return_seconds=False)
            if speech and "start" in speech and speech_started_at is None:
                speech_started_at = time.monotonic()
                recorded_chunks.extend(pre_roll)
                pre_roll.clear()
                if on_speech_start:
                    on_speech_start()
            elif speech and "end" in speech and speech_started_at is not None:
                if trace is not None:
                    detected = time.perf_counter_ns()
                    # Silero returns a padded sample index. Relate it to the
                    # delivered input blocks; device/acoustic latency is unknown.
                    end = max(0, speech["end"] - getattr(vad_iterator, "speech_pad_samples", 3200))
                    estimate = detected - int(max(0, samples_read - end) * 1e9 / SAMPLE_RATE)
                    trace.mark("speech_end_estimate", estimate)
                    trace.mark("vad_detected", detected)
                break
            if (
                speech_started_at is None and idle_interrupt_event is not None
                and idle_interrupt_event.is_set()
            ):
                return None

    check_cancelled(stop_event)
    if speech_started_at is None or not recorded_chunks:
        return None
    mark("capture_stopped")
    with span("capture_assembly"):
        audio = np.concatenate(recorded_chunks, axis=0)[:, 0]
    mark("capture_complete")
    return audio


def transcribe_audio(audio, cancel_event=None):
    mark("stt_started")
    with span("stt"):
        text = _transcribe_audio(audio, cancel_event)
    mark("stt_complete")
    return text


def _transcribe_audio(audio, cancel_event=None):
    if audio is None:
        return ""
    check_cancelled(cancel_event)
    # faster-whisper eagerly filters/extracts features, then lazily decodes
    # while consuming segments. Keep those wall/CPU spans separate.
    with span("stt_frontend"):
        segments, _info = whisper_model.transcribe(
            audio, language=WHISPER_LANGUAGE, beam_size=max(1, min(10, WHISPER_BEAM_SIZE)),
            condition_on_previous_text=False,
            without_timestamps=config.WHISPER_WITHOUT_TIMESTAMPS,
            vad_filter=True,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
    text = []
    previous = ""
    with span("stt_decode"):
        for segment in segments:
            check_cancelled(cancel_event)
            if (
                getattr(segment, "avg_logprob", 0) < -1.5
                or getattr(segment, "compression_ratio", 1) > 2.4
                or (getattr(segment, "no_speech_prob", 0) > 0.6
                    and getattr(segment, "avg_logprob", 0) < -0.5)
            ):
                logging.info("Ignored an unreliable speech segment")
                continue
            current = segment.text.strip()
            if len(current) >= 18 and current.casefold() == previous:
                continue
            text.append(segment.text)
            previous = current.casefold()
    check_cancelled(cancel_event)
    return "".join(text).strip()

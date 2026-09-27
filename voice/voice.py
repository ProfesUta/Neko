import os
import warnings
from pathlib import Path

import numpy as np
import sounddevice as sd
import torch

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"torch\.jit\._serialization"
)

from silero_vad import load_silero_vad, VADIterator
from faster_whisper import WhisperModel


SAMPLE_RATE = 16000

# Audio chunk used by Silero VAD.
# 512 samples at 16 kHz = 32 ms.
VAD_CHUNK_SIZE = 512

# How much silence must pass before we consider speech finished.
SILENCE_DURATION_MS = 700

# Maximum time to wait for someone to start speaking.
WAIT_TIMEOUT_SECONDS = 30


if os.name == "nt":
    site_packages = Path(
        os.environ.get(
            "APPDATA",
            ""
        )
    ) / "Python" / "Python314" / "site-packages"

    cuda_directories = [
        site_packages / "nvidia" / "cublas" / "bin",
        site_packages / "nvidia" / "cudnn" / "bin",
        site_packages / "nvidia" / "cuda_nvrtc" / "bin",
    ]

    existing_directories = []

    for directory in cuda_directories:
        if directory.exists():
            existing_directories.append(str(directory))

            try:
                os.add_dll_directory(str(directory))
            except Exception:
                pass

    if existing_directories:
        os.environ["PATH"] = (
            os.pathsep.join(existing_directories)
            + os.pathsep
            + os.environ.get("PATH", "")
        )


print("Loading Whisper model...")

whisper_model = WhisperModel(
    "base",
    device="cuda",
    compute_type="float16"
)

print("Whisper is ready!")
print()

print("Loading Silero VAD...")

vad_model = load_silero_vad()
vad_iterator = VADIterator(
    vad_model,
    threshold=0.5,
    sampling_rate=SAMPLE_RATE,
    min_silence_duration_ms=SILENCE_DURATION_MS,
    speech_pad_ms=200
)

print("Silero VAD is ready!")
print()


def record_audio():
    print("🎤 Listening...")

    audio_chunks = []
    speech_started = False
    silence_started = False

    def callback(indata, frames, time, status):
        if status:
            print(status)

        audio_chunks.append(indata.copy())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=VAD_CHUNK_SIZE,
        callback=callback
    )

    vad_iterator.reset_states()

    with stream:
        elapsed_seconds = 0

        while True:
            if not audio_chunks:
                continue

            chunk = audio_chunks.pop(0)

            audio = torch.from_numpy(
                chunk[:, 0]
            )

            speech_dict = vad_iterator(
                audio,
                return_seconds=False
            )

            if speech_dict:
                if "start" in speech_dict:
                    speech_started = True
                    silence_started = False

                    print("🗣️ Speech detected!")

                elif "end" in speech_dict:
                    if speech_started:
                        print("🤫 Speech ended.")
                        break

            elapsed_seconds += len(chunk) / SAMPLE_RATE

            if not speech_started and elapsed_seconds >= WAIT_TIMEOUT_SECONDS:
                print("⏱️ No speech detected.")
                return None

    if not audio_chunks and not speech_started:
        return None

    # The VAD iterator consumes the chunks while detecting speech.
    # For Whisper we need the complete recording, so this function
    # records through a second pass below.
    return None


def record_until_silence():
    print("🎤 Listening...")

    recorded_chunks = []

    speech_started = False
    silence_chunks = 0

    silence_chunks_needed = int(
        SILENCE_DURATION_MS / 1000
        * SAMPLE_RATE
        / VAD_CHUNK_SIZE
    )

    vad_iterator.reset_states()

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=VAD_CHUNK_SIZE
    ) as stream:

        while True:
            chunk, overflowed = stream.read(VAD_CHUNK_SIZE)

            if overflowed:
                print("⚠️ Audio buffer overflow.")

            chunk = chunk.copy()

            recorded_chunks.append(chunk)

            audio = torch.from_numpy(
                chunk[:, 0]
            )

            speech_dict = vad_iterator(
                audio,
                return_seconds=False
            )

            if speech_dict:
                if "start" in speech_dict:
                    speech_started = True
                    silence_chunks = 0
                    print("🗣️ Speech detected!")

                elif "end" in speech_dict:
                    if speech_started:
                        print("🤫 Speech ended.")
                        break

            if speech_started:
                speech_probability = vad_model(
                    audio,
                    SAMPLE_RATE
                ).item()

                if speech_probability >= 0.5:
                    silence_chunks = 0
                else:
                    silence_chunks += 1

                if silence_chunks >= silence_chunks_needed:
                    print("🤫 Speech ended.")
                    break

    if not speech_started:
        return None

    audio = np.concatenate(
        recorded_chunks,
        axis=0
    )

    return audio[:, 0]


def transcribe_audio(audio):
    if audio is None:
        return ""

    print("📝 Transcribing...")

    segments, info = whisper_model.transcribe(
        audio,
        language="en",
        beam_size=5
    )

    text = ""

    for segment in segments:
        text += segment.text

    return text.strip()
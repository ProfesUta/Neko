"""Resident CPU Kokoro synthesis process; its JSON protocol never plays audio."""

import json
import math
import sys
import wave
import time
from pathlib import Path


ASSET_FILES = ("model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt")
CHAMPION_LEXICON = Path(__file__).with_name("champion-pronunciations.lexicon")


def validate_assets(asset_dir):
    directory = Path(asset_dir).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"Kokoro voice directory does not exist: {directory}")
    for name in ASSET_FILES:
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Kokoro voice asset is missing or empty: {name}")
    if not (directory / "espeak-ng-data").is_dir():
        raise ValueError("Kokoro voice asset is missing: espeak-ng-data")
    return directory


def validate_settings(speaker=53, speed=1.1):
    if isinstance(speaker, bool) or not isinstance(speaker, int) or not 0 <= speaker <= 1023:
        raise ValueError("Kokoro speaker must be an integer between 0 and 1023")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ValueError("Kokoro speed must be a number between 0.5 and 2.0")
    if not 0.5 <= speed <= 2.0:
        raise ValueError("Kokoro speed must be a finite number between 0.5 and 2.0")
    speed = float(speed)
    if not math.isfinite(speed):
        raise ValueError("Kokoro speed must be a finite number between 0.5 and 2.0")
    return speaker, speed


def validate_request(text, output, speaker=53, speed=1.1):
    speaker, speed = validate_settings(speaker, speed)
    if not isinstance(text, str) or not text.strip() or len(text) > 10000 or "\x00" in text:
        raise ValueError("Kokoro text must contain 1 to 10000 characters without NUL bytes")
    if not isinstance(output, (str, Path)) or not str(output).strip():
        raise ValueError("Kokoro requires an output WAV path")
    output = Path(output).expanduser().resolve()
    if not output.parent.is_dir() or output.is_dir():
        raise ValueError("Kokoro output must be a file in an existing directory")
    return text, output, speaker, speed


def create_tts(asset_dir, num_threads=2):
    # Import only in the isolated worker. Never load the native DLLs in a voice pack.
    import sherpa_onnx

    directory = validate_assets(asset_dir)
    if type(num_threads) is not int or not 1 <= num_threads <= 8:
        raise ValueError("Kokoro thread count must be between 1 and 8")
    if not CHAMPION_LEXICON.is_file():
        raise ValueError("Capri's champion pronunciation dictionary is missing")
    model = sherpa_onnx.OfflineTtsKokoroModelConfig(
        model=str(directory / "model.onnx"),
        voices=str(directory / "voices.bin"),
        tokens=str(directory / "tokens.txt"),
        lexicon=",".join((str(directory / "lexicon-us-en.txt"), str(CHAMPION_LEXICON))),
        data_dir=str(directory / "espeak-ng-data"),
        lang="en-us",
    )
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(kokoro=model, num_threads=num_threads, provider="cpu"),
        max_num_sentences=1,
    )
    if not config.validate():
        raise ValueError("The Kokoro voice pack could not be validated")
    return sherpa_onnx.OfflineTts(config)


def synthesize(tts, request, on_chunk=None):
    import numpy as np

    text, output, speaker, speed = validate_request(
        request.get("text"), request.get("output"), request.get("speaker", 53), request.get("speed", 1.1)
    )
    if speaker >= tts.num_speakers:
        raise ValueError(f"Kokoro voice has {tts.num_speakers} speakers; speaker {speaker} is unavailable")
    chunk_count = 0
    def chunk(samples, progress):
        nonlocal chunk_count
        path = output.with_name(f"{output.stem}-{chunk_count}.wav")
        chunk_count += 1
        values = np.asarray(samples, dtype=np.float32)
        if values.ndim != 1 or not values.size or not np.isfinite(values).all():
            raise RuntimeError("Kokoro produced an invalid audio chunk")
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, tts.sample_rate, 0, "NONE", "not compressed"))
            stream.writeframes((np.clip(values, -1, 1) * 32767).astype("<i2").tobytes())
        on_chunk({"chunk": str(path), "frames": int(values.size), "sample_rate": tts.sample_rate})
        return 1
    options = {"callback": chunk} if on_chunk is not None else {}
    profiled = request.get("profile") is True
    wall, cpu = (time.perf_counter_ns(), time.process_time_ns()) if profiled else (0, 0)
    audio = tts.generate(text, sid=speaker, speed=speed, **options)
    samples = np.asarray(audio.samples, dtype=np.float32)
    sample_rate = int(audio.sample_rate)
    if samples.ndim != 1 or samples.size == 0 or not np.isfinite(samples).all():
        raise RuntimeError("Kokoro produced invalid or empty audio")
    if not 8000 <= sample_rate <= 192000:
        raise RuntimeError("Kokoro produced an invalid sample rate")
    if on_chunk is None or chunk_count == 0:
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(str(output), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(pcm.tobytes())
        if on_chunk is not None:
            on_chunk({"chunk": str(output), "frames": int(samples.size), "sample_rate": sample_rate})
    result = {"ok": True, "sample_rate": sample_rate, "frames": int(samples.size)}
    if profiled:
        result.update(synthesis_wall_ns=time.perf_counter_ns() - wall, synthesis_cpu_ns=time.process_time_ns() - cpu)
    return result


def main():
    tts = create_tts(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    print(json.dumps({"ready": True, "speakers": tts.num_speakers, "sample_rate": tts.sample_rate}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Kokoro request must be a JSON object")
            emit = lambda message: print(json.dumps(message), flush=True)
            response = synthesize(tts, request, on_chunk=emit if request.get("stream") is True else None)
        except Exception as error:
            response = {"error": str(error)}
        print(json.dumps(response, allow_nan=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}), flush=True)
        sys.exit(1)

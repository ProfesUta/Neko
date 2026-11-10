"""Resident CPU Kokoro synthesis process; its JSON protocol never plays audio."""

import json
import math
import sys
import wave
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


def create_tts(asset_dir):
    # Import only in the isolated worker. Never load the native DLLs in a voice pack.
    import sherpa_onnx

    directory = validate_assets(asset_dir)
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
        model=sherpa_onnx.OfflineTtsModelConfig(kokoro=model, num_threads=2, provider="cpu"),
        max_num_sentences=1,
    )
    if not config.validate():
        raise ValueError("The Kokoro voice pack could not be validated")
    return sherpa_onnx.OfflineTts(config)


def synthesize(tts, request):
    import numpy as np

    text, output, speaker, speed = validate_request(
        request.get("text"), request.get("output"), request.get("speaker", 53), request.get("speed", 1.1)
    )
    if speaker >= tts.num_speakers:
        raise ValueError(f"Kokoro voice has {tts.num_speakers} speakers; speaker {speaker} is unavailable")
    audio = tts.generate(text, sid=speaker, speed=speed)
    samples = np.asarray(audio.samples, dtype=np.float32)
    sample_rate = int(audio.sample_rate)
    if samples.ndim != 1 or samples.size == 0 or not np.isfinite(samples).all():
        raise RuntimeError("Kokoro produced invalid or empty audio")
    if not 8000 <= sample_rate <= 192000:
        raise RuntimeError("Kokoro produced an invalid sample rate")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm.tobytes())
    return {"ok": True, "sample_rate": sample_rate, "frames": int(samples.size)}


def main():
    tts = create_tts(sys.argv[1])
    print(json.dumps({"ready": True, "speakers": tts.num_speakers, "sample_rate": tts.sample_rate}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Kokoro request must be a JSON object")
            response = synthesize(tts, request)
        except Exception as error:
            response = {"error": str(error)}
        print(json.dumps(response, allow_nan=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}), flush=True)
        sys.exit(1)

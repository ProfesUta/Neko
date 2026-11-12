"""Resident CPU Kokoro synthesis process; its JSON protocol never plays audio."""

import json
import math
import sys
import wave
import time
import argparse
import tempfile
from pathlib import Path


ASSET_FILES = ("model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt")
CHAMPION_LEXICON = Path(__file__).with_name("champion-pronunciations.lexicon")
CONVERSATION_LEXICON = Path(__file__).with_name("conversation-pronunciations.lexicon")


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


def create_tts(asset_dir, num_threads=2, cpu_options=None, personal_config=None):
    if personal_config is None:
        return _create_native_tts(asset_dir, num_threads, cpu_options)
    # The script worker has voice/ on sys.path; package imports are used in tests.
    if __package__:
        from .personal_pronunciation import compile_profile, PersonalTts
    else:
        from personal_pronunciation import compile_profile, PersonalTts
    profile = compile_profile(personal_config, validate_assets(asset_dir), CHAMPION_LEXICON)
    if not profile.replacements:
        return _create_native_tts(asset_dir, num_threads, cpu_options)
    # Native Kokoro copies the lexicon at construction. No persistent plaintext
    # derivative or changes to original model assets are needed.
    with tempfile.TemporaryDirectory(prefix="capri-personal-lexicon-") as directory:
        dictionary = Path(directory) / "personal.lexicon"
        dictionary.write_text(profile.lexicon, encoding="utf-8")
        engine = _create_native_tts(asset_dir, num_threads, cpu_options, dictionary)
    return PersonalTts(engine, profile)


def _create_native_tts(asset_dir, num_threads=2, cpu_options=None, personal_lexicon=None):
    # Import only in the isolated worker. Never load the native DLLs in a voice pack.
    import sherpa_onnx

    directory = validate_assets(asset_dir)
    if type(num_threads) is not int or not 1 <= num_threads <= 8:
        raise ValueError("Kokoro thread count must be between 1 and 8")
    if not CHAMPION_LEXICON.is_file():
        raise ValueError("Capri's champion pronunciation dictionary is missing")
    if not CONVERSATION_LEXICON.is_file():
        raise ValueError("Capri's conversation pronunciation dictionary is missing")
    model = sherpa_onnx.OfflineTtsKokoroModelConfig(
        model=str(directory / "model.onnx"),
        voices=str(directory / "voices.bin"),
        tokens=str(directory / "tokens.txt"),
        lexicon=",".join((str(directory / "lexicon-us-en.txt"), str(CONVERSATION_LEXICON), str(CHAMPION_LEXICON),
                          *((str(personal_lexicon),) if personal_lexicon else ()))),
        data_dir=str(directory / "espeak-ng-data"),
        lang="en-us",
    )
    provider = "cpu"
    if cpu_options:
        path = Path(cpu_options).resolve()
        if not path.is_file() or path.stat().st_size > 16384:
            print("Kokoro CPU options unavailable; retaining plain CPU", file=sys.stderr, flush=True)
            path = None
        version = tuple(int(part) for part in str(getattr(sherpa_onnx, "__version__", "0")).split(".")[:3] if part.isdigit())
        if path is not None and version >= (2, 0, 1):
            provider = "cpu:" + str(path)
        else:
            print("Kokoro CPU session options unavailable in this runtime; retaining plain CPU", file=sys.stderr, flush=True)
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(kokoro=model, num_threads=num_threads, provider=provider),
        max_num_sentences=1,
    )
    if not config.validate():
        raise ValueError("The Kokoro voice pack could not be validated")
    try:
        return sherpa_onnx.OfflineTts(config)
    except RuntimeError:
        if provider == "cpu":
            raise
        print("Kokoro CPU tuning unavailable; retrying plain CPU", file=sys.stderr, flush=True)
        config.model.provider = "cpu"
        return sherpa_onnx.OfflineTts(config)


def synthesize(tts, request, on_chunk=None):
    import numpy as np

    text, output, speaker, speed = validate_request(
        request.get("text"), request.get("output"), request.get("speaker", 53), request.get("speed", 1.1)
    )
    if speaker >= tts.num_speakers:
        raise ValueError(f"Kokoro voice has {tts.num_speakers} speakers; speaker {speaker} is unavailable")
    chunk_count = 0
    profiled = request.get("profile") is True
    first_callback_cpu = None
    def chunk(samples, progress):
        nonlocal chunk_count, first_callback_cpu
        callback_at = time.perf_counter_ns() if profiled else 0
        if profiled and first_callback_cpu is None:
            first_callback_cpu = time.process_time_ns() - cpu
        path = output.with_name(f"{output.stem}-{chunk_count}.wav")
        chunk_count += 1
        values = np.asarray(samples, dtype=np.float32)
        if values.ndim != 1 or not values.size or not np.isfinite(values).all():
            raise RuntimeError("Kokoro produced an invalid audio chunk")
        pcm = (np.clip(values, -1, 1) * 32767).astype("<i2").tobytes()
        converted_at = time.perf_counter_ns() if profiled else 0
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, tts.sample_rate, 0, "NONE", "not compressed"))
            stream.writeframes(pcm)
        message = {"chunk": str(path), "frames": int(values.size), "sample_rate": tts.sample_rate,
                   "generation_complete": bool(progress >= 1.0)}
        if profiled:
            message["timing"] = {"generate_enter_ns": wall, "callback_ns": callback_at,
                                 "pcm_ns": converted_at, "wav_ns": time.perf_counter_ns()}
        on_chunk(message)
        return 1
    options = {"callback": chunk} if on_chunk is not None else {}
    wall, cpu = (time.perf_counter_ns(), time.process_time_ns()) if profiled else (0, 0)
    audio = tts.generate(text, sid=speaker, speed=speed, **options)
    returned_at = time.perf_counter_ns() if profiled else 0
    samples = np.asarray(audio.samples, dtype=np.float32)
    sample_rate = int(audio.sample_rate)
    if samples.ndim != 1 or samples.size == 0 or not np.isfinite(samples).all():
        raise RuntimeError("Kokoro produced invalid or empty audio")
    if not 8000 <= sample_rate <= 192000:
        raise RuntimeError("Kokoro produced an invalid sample rate")
    if on_chunk is None or chunk_count == 0:
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        converted_at = time.perf_counter_ns() if profiled else 0
        with wave.open(str(output), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(pcm.tobytes())
        written_at = time.perf_counter_ns() if profiled else 0
        if on_chunk is not None:
            on_chunk({"chunk": str(output), "frames": int(samples.size), "sample_rate": sample_rate,
                      "generation_complete": True})
    result = {"ok": True, "sample_rate": sample_rate, "frames": int(samples.size)}
    if profiled:
        result.update(synthesis_wall_ns=time.perf_counter_ns() - wall, synthesis_cpu_ns=time.process_time_ns() - cpu)
        result["timing"] = {"generate_enter_ns": wall, "generate_return_ns": returned_at}
        if on_chunk is None or chunk_count == 0:
            result["timing"].update(pcm_ns=converted_at, wav_ns=written_at)
        if first_callback_cpu is not None:
            result["first_callback_cpu_ns"] = first_callback_cpu
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_dir")
    parser.add_argument("num_threads", type=int, nargs="?", default=2)
    parser.add_argument("cpu_options", nargs="?")
    parser.add_argument("--personal-pronunciations", type=Path)
    args = parser.parse_args()
    tts = create_tts(args.asset_dir, args.num_threads, args.cpu_options, args.personal_pronunciations)
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

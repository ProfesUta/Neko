"""Direct Kokoro A/B verification; no LLM, microphone, chat or screen access.

--configure prompts locally, plays a candidate and saves only after listening
confirmation. Otherwise supply an existing private --config; --audio plays it.
Output audio/optional traces live in ignored artifacts/, not public logs.
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

import config
from core.latency import VoiceMetrics
from voice import tts
from voice.kokoro_session import session, KokoroSession
from voice.kokoro_worker import CHAMPION_LEXICON
from voice.personal_pronunciation import compile_profile, read_profile
from voice.pronunciation import prepare_kokoro_text


def pcm_info(path):
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    return {"frames": len(pcm), "sample_rate": rate, "duration_s": len(pcm)/rate,
            "rms": float(np.sqrt(np.mean((pcm.astype(float)/32768)**2)))}


def paired_latency(profile_path, output, samples):
    """Two temporary instances of the SAME worker, benchmark-only, AB/BA order."""
    original = config.PERSONAL_PRONUNCIATIONS_FILE
    workers = {name: KokoroSession() for name in ("baseline", "profile")}
    metrics = {name: VoiceMetrics(True) for name in workers}
    try:
        for trial in range(samples + 1):
            for name in (("baseline", "profile") if trial % 2 else ("profile", "baseline")):
                config.PERSONAL_PRONUNCIATIONS_FILE = profile_path if name == "profile" else None
                worker = workers[name]
                worker.request(config.KOKORO_VOICE_DIR)  # Loading is outside warm timing.
                start = time.perf_counter_ns()
                path = output / f"{name}-control.wav"
                worker.request(config.KOKORO_VOICE_DIR, text="I am here.", output=path, **tts.kokoro_settings())
                if trial:
                    metrics[name].record("unchanged_text_to_audio_ready", time.perf_counter_ns()-start)
                path.unlink(missing_ok=True)
    finally:
        for worker in workers.values():
            worker.shutdown()
        config.PERSONAL_PRONUNCIATIONS_FILE = original
    return {name: value.report() for name, value in metrics.items()}


def compare(profile_path, output, audio=False, trace=False, latency_samples=0):
    entries = read_profile(profile_path)
    if not entries:
        raise ValueError("Enable at least one entry to verify its pronunciation")
    profile = compile_profile(profile_path, config.KOKORO_VOICE_DIR, CHAMPION_LEXICON)
    output.mkdir(parents=True, exist_ok=True)
    displayed = entries[0]["display_name"]
    texts = [displayed + ".", f"{displayed}, I am here.", f"Hello, {displayed}, I am here.",
             f"I am here, {displayed}.", f'Hello, "{displayed}"!', f"{displayed}'s turn.",
             f"Hello. {displayed}. I am here."]
    summary = {"voice": tts.kokoro_settings(), "cases": [], "latency": {}, "recent_native_oov_messages": {}}
    private_trace = []
    original = config.PERSONAL_PRONUNCIATIONS_FILE
    try:
        for label, selected in (("baseline", None), ("profile", profile_path)):
            config.PERSONAL_PRONUNCIATIONS_FILE = selected
            session.shutdown()
            start = time.perf_counter_ns()
            tts.warm_up()
            cold_ms = (time.perf_counter_ns()-start)/1e6
            for index, text in enumerate(texts):
                cleaned = tts.clean_for_speech(text)
                prepared = prepare_kokoro_text(cleaned)
                for streaming in (False, True):
                    path = output / f"{label}-{index}-{'stream' if streaming else 'full'}.wav"
                    delivered, rates = [], []
                    def ready(message):
                        with wave.open(message["chunk"], "rb") as source:
                            delivered.append(source.readframes(source.getnframes()))
                            rates.append(source.getframerate())
                        Path(message["chunk"]).unlink(missing_ok=True)
                    session.request(config.KOKORO_VOICE_DIR, text=prepared, output=path,
                                    on_chunk=ready if streaming else None, **tts.kokoro_settings())
                    if streaming:
                        if not rates or len(set(rates)) != 1:
                            raise RuntimeError("Stream delivered no PCM or changed sample rate")
                        with wave.open(str(path), "wb") as target:
                            target.setparams((1, 2, rates[0], 0, "NONE", "not compressed"))
                            target.writeframes(b"".join(delivered))
                    summary["cases"].append({"mode": label, "fixture": index, "streaming": streaming,
                                              "chunks": len(delivered) if streaming else None, **pcm_info(path)})
                    if audio:
                        print(f"Playing {label}, fixture {index + 1}, {'streamed' if streaming else 'full'}", flush=True)
                        tts._play_wav(path, None)
                private_trace.append({"mode": label, "fixture": index, "display": text, "cleaned": cleaned,
                                      "prepared": prepared, "native_input": profile.apply(prepared) if selected else prepared})
            # The native diagnostic ring is bounded; these are retained-message
            # counts, not a total log count. Never export the messages/names.
            summary["recent_native_oov_messages"][label] = sum("OOV word skipped" in line for line in session._diagnostics)
            summary["latency"][label] = {"cold_ready_ms": cold_ms}
    finally:
        session.shutdown()
        config.PERSONAL_PRONUNCIATIONS_FILE = original
    if latency_samples:
        summary["paired_warm_latency"] = paired_latency(profile_path, output, latency_samples)
    (output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    if trace:
        # Explicitly requested private diagnostic, never console/application logs.
        (output / "private-trace.json").write_text(json.dumps(private_trace, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    return summary


def configure(destination, output):
    print("This plays the existing voice. Use known dictionary words or exact model phoneme tokens; neither guarantees your preferred sound.")
    name = input("Displayed name (exact GUI spelling): ").strip()
    spoken = input("Preferred spoken spelling using dictionary words (blank for phonemes): ").strip()
    phones = input("Optional space-separated model phonemes (blank uses the spelling): ").strip()
    entry = {"display_name": name, "spoken_pronunciation": spoken, "phonemes": phones.split() if phones else None}
    # Keep unrelated private entries when reconfiguring an existing name.
    existing = []
    if destination.is_file():
        read_profile(destination)  # Validate without echoing private contents.
        existing = json.loads(destination.read_text(encoding="utf-8"))["entries"]
    entries = [entry, *(value for value in existing if value.get("display_name", "").strip().casefold() != name.casefold())]
    with tempfile.TemporaryDirectory(prefix="capri-pronunciation-check-") as directory:
        candidate = Path(directory) / "candidate.json"
        candidate.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
        compile_profile(candidate, config.KOKORO_VOICE_DIR, CHAMPION_LEXICON)
        compare(candidate, output, audio=True)
        if input("Keep this pronunciation after listening? [y/N]: ").strip().casefold() != "y":
            print("Not activated. Adjust it and run --configure again.")
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        shutil.copyfile(candidate, temporary)
        temporary.replace(destination)
    print("Private pronunciation saved. The warm worker reloads it on the next request.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=config.PERSONAL_PRONUNCIATIONS_FILE)
    parser.add_argument("--output", type=Path, default=Path("artifacts/pronunciation/verification"))
    parser.add_argument("--configure", action="store_true", help="Prompt locally, play A/B, then ask before activation")
    parser.add_argument("--audio", action="store_true", help="Explicitly play saved direct full/streamed fixtures")
    parser.add_argument("--trace", action="store_true", help="Save name/text/native input only to the private output directory")
    parser.add_argument("--tokens", action="store_true", help="Show the installed model's accepted symbol inventory")
    parser.add_argument("--latency-samples", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.latency_samples <= 100:
        parser.error("--latency-samples must be 0..100")
    if args.tokens:
        sys.stdout.reconfigure(encoding="utf-8")
        print((config.KOKORO_VOICE_DIR / "tokens.txt").read_text(encoding="utf-8"))
        return
    if args.config is None or str(args.config).casefold() == "none":
        parser.error("Select a private profile file with --config before configuring/verifying")
    try:
        if args.configure:
            configure(args.config, args.output)
        else:
            compare(args.config, args.output, args.audio, args.trace, args.latency_samples)
            print("Verification audio and numeric summary saved. Use --audio to listen; no pronunciation was activated.")
    except ValueError as error:
        parser.exit(2, str(error)+"\n")


if __name__ == "__main__":
    main()

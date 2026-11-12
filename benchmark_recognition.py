"""Explicit offline recognition/VAD experiments; never opens a microphone.

Default corpus is generated speech with known pauses/noise, not human accuracy
validation. --manifest accepts recorded 16k/PCM WAVs with reference text and a
manually annotated speech_end_sample. Normal application settings are untouched.
"""
import argparse
import gc
import os
import json
import re
import statistics
import time
import wave
from pathlib import Path
from threading import Event

import numpy as np
import config
from core.latency import VoiceMetrics, LatencyTrace, trace_scope
from benchmark_voice import read_recording, hardware
from voice import voice


def words(text):
    return re.findall(r"\w+", text.casefold())


def edit_distance(reference, actual):
    previous = list(range(len(actual) + 1))
    for row, word in enumerate(reference, 1):
        current = [row]
        for column, other in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (word != other)))
        previous = current
    return previous[-1]


def save_pcm(path, pcm):
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())


def corpus(directory):
    """Stable saved fixtures; subsequent runs reuse identical PCM."""
    manifest = directory / "manifest.json"
    if manifest.is_file():
        return manifest
    from voice import tts
    from voice.kokoro_session import session
    directory.mkdir(parents=True, exist_ok=True)
    texts = {"short": "What time is it?", "long": "Could you explain why a planet stays in orbit around the sun instead of falling straight into it?",
             "part1": "Could you", "part2": "explain gravity to me?", "quiet": "Where is my browser window?"}
    signals = {}
    try:
        for key, text in texts.items():
            path = directory / f"source-{key}.wav"
            tts._synthesize(text, path, Event())
            signals[key] = read_recording(path)
    finally:
        session.shutdown()
    records = []
    def add(name, pcm, text):
        # Annotation precedes augmentation; noise must not redefine speech end.
        end = int(np.flatnonzero(np.abs(pcm) > 32 / 32768)[-1]) + 3200
        padded = np.concatenate((np.zeros(3200), pcm, np.zeros(16000))).astype("float32")
        records.append({"name": name, "path": name + ".wav", "reference": text,
                        "speech_end_sample": end})
        return padded
    for name in ("short", "long", "quiet"):
        pcm = add(name, signals[name], texts[name])
        if name == "quiet":
            pcm *= .05
        save_pcm(directory / f"{name}.wav", pcm)
    def trim(pcm):
        active = np.flatnonzero(np.abs(pcm) > 32 / 32768)
        return pcm[max(0, active[0]-800):active[-1]+800]
    for pause in (350, 550):
        combined = np.concatenate((trim(signals["part1"]), np.zeros(int(pause * 16)), trim(signals["part2"])))
        name = f"pause{pause}"
        save_pcm(directory / f"{name}.wav", add(name, combined, texts["part1"] + " " + texts["part2"]))
    clean = add("noise", signals["quiet"], texts["quiet"])
    rng = np.random.default_rng(417)
    save_pcm(directory / "noise.wav", clean + rng.normal(0, .008, len(clean)).astype("float32"))
    for name, pcm in (("silence", np.zeros(32000)), ("noise_only", rng.normal(0, .008, 32000))):
        save_pcm(directory / f"{name}.wav", pcm)
        records.append({"name": name, "path": name + ".wav", "reference": "", "speech_end_sample": None})
    manifest.write_text(json.dumps({"kind": "generated speech, inserted pauses/quiet/noise; not human recordings", "fixtures": records}, indent=2))
    return manifest


def load_corpus(manifest, selected=None):
    data = json.loads(manifest.read_text(encoding="utf-8"))
    fixtures = []
    for item in data["fixtures"]:
        if selected and item["name"] not in selected:
            continue
        pcm = read_recording(manifest.parent / item["path"])
        fixtures.append((item, pcm))
    if not fixtures:
        raise ValueError("No selected fixtures")
    return data["kind"], fixtures


def vad_bench(fixtures, samples):
    import torch
    from silero_vad import VADIterator, load_silero_vad
    model = load_silero_vad()
    result = {}
    for silence in (300, 400, 500):
        detector = VADIterator(model, threshold=.5, sampling_rate=16000,
                               min_silence_duration_ms=silence, speech_pad_ms=200)
        cases = {}
        for item, pcm in fixtures:
            metrics, early, missed, endpoint_delays = VoiceMetrics(True), 0, 0, []
            for trial in range(samples + 1):
                detector.reset_states()
                endpoint = None
                begin = time.perf_counter_ns()
                for end in range(512, len(pcm) + 1, 512):
                    detected = detector(torch.from_numpy(pcm[end-512:end]), return_seconds=False)
                    if detected and "end" in detected:
                        endpoint = end
                        break
                if not trial:
                    continue
                metrics.record("vad_offline_compute", time.perf_counter_ns() - begin)
                expected = item["speech_end_sample"]
                if expected is not None and endpoint is not None:
                    early += endpoint < expected
                    if endpoint >= expected:
                        delay = (endpoint - expected) / 16000
                        metrics.record("annotated_speech_end_to_endpoint", delay * 1e9)
                        endpoint_delays.append(delay * 1000)
                elif expected is not None:
                    missed += 1
                else:
                    early += endpoint is not None  # False endpoint on annotated nonspeech.
            cases[item["name"]] = {"metrics": metrics.report(), "premature_or_false_endpoints": early,
                "missed_endpoints": missed, "samples": samples,
                "endpoint_delay_variance_ms2": statistics.pvariance(endpoint_delays) if endpoint_delays else None}
        result[str(silence)] = cases
    return {"kind": "real Silero on saved PCM; sample-index latency, offline compute separately", "cases": result}


class _RecognitionProbe:
    def __init__(self, model, filter_enabled):
        self.model, self.filter_enabled = model, filter_enabled

    def transcribe(self, audio, **kwargs):
        return self.model.transcribe(audio, **{**kwargs, "vad_filter": self.filter_enabled})


def recognition_bench(fixtures, samples, threads, compare_filter):
    from faster_whisper import WhisperModel
    # An isolated benchmark process owns these test models, one at a time.
    # No change to application model reuse or default VAD filtering.
    result = {}
    for count in threads:
        model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=count, local_files_only=True)
        for filtered in ((True, False) if compare_filter else (True,)):
            cases = {}
            voice.whisper_model = _RecognitionProbe(model, filtered)
            for item, pcm in fixtures:
                metrics, elapsed, errors, hallucinations = VoiceMetrics(True), [], [], 0
                for trial in range(samples + 1):
                    collector = metrics if trial else VoiceMetrics(True)
                    start = time.perf_counter_ns()
                    with trace_scope(LatencyTrace(collector)):
                        answer = voice.transcribe_audio(pcm, Event())
                    if trial:
                        elapsed.append((time.perf_counter_ns() - start) / 1e6)
                        errors.append(edit_distance(words(item["reference"]), words(answer)))
                        hallucinations += not item["reference"] and bool(answer)
                cases[item["name"]] = {"metrics": metrics.report(), "reference_words": len(words(item["reference"])),
                    "edit_errors_min_max": [min(errors), max(errors)], "hallucinated_nonspeech": hallucinations,
                    "latency_variance_ms2": statistics.pvariance(elapsed), "samples": samples}
                print(f"Recognition: threads={count} filter={filtered} fixture={item['name']}", flush=True)
            result[f"threads{count}_filter{int(filtered)}"] = cases
        voice.whisper_model = None
        del model
        gc.collect()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--corpus-dir", type=Path, default=Path("artifacts/performance/phase4-corpus"))
    parser.add_argument("--fixtures", nargs="+")
    parser.add_argument("--threads", nargs="+", type=int, default=[2, 4, 8])
    parser.add_argument("--compare-filter", action="store_true")
    parser.add_argument("--passive-wait", action="store_true", help="Explicit isolated-process OpenMP wait-policy experiment")
    parser.add_argument("--recognition", action="store_true")
    parser.add_argument("--vad", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or any(count not in (1, 2, 4, 6, 8) for count in args.threads):
        parser.error("Use 1..100 samples and threads 1,2,4,6,8")
    if args.passive_wait:
        os.environ["OMP_WAIT_POLICY"] = "PASSIVE"
        os.environ["KMP_BLOCKTIME"] = "0"
    manifest = args.manifest or corpus(args.corpus_dir)
    kind, fixtures = load_corpus(manifest, args.fixtures)
    result = {"corpus": kind, "manifest": str(manifest), "hardware": hardware(), "samples": args.samples,
              "model": config.WHISPER_MODEL, "device": "cpu", "compute_type": "int8"}
    result["thread_environment"] = {key: os.environ.get(key) for key in ("OMP_WAIT_POLICY", "KMP_BLOCKTIME")}
    if args.vad:
        result["vad"] = vad_bench(fixtures, args.samples)
    if args.recognition:
        result["recognition"] = recognition_bench(fixtures, args.samples, args.threads, args.compare_filter)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Saved", args.output)


if __name__ == "__main__":
    main()

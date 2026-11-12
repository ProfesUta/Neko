"""Kokoro CPU/transport profiling. Silent unless --audio is explicitly supplied.

Uses Capri's existing resident worker, pronunciation, chunk ownership and playback.
--stress deliberately runs local Qwen32/vision requests concurrently; this is a
resource stress test, not Capri's normal foreground admission policy.
"""
import argparse
import hashlib
import json
import os
import statistics
import tempfile
import time
import wave
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Thread

import numpy as np
import psutil

import config
from core.cancellation import OperationCancelled
from core.latency import LatencyTrace, VoiceMetrics, trace_scope
from voice import tts
from voice.kokoro_session import session

FIXTURES = {
    "short": "Hey, I'm right here.",
    "medium": "Gravity pulls objects with mass toward each other.",
    "long": "Gravity pulls objects with mass toward each other. The pull gets weaker as the distance grows. It keeps planets moving around the sun.",
    "league": "Your team locked Yuumi and Nautilus.",
    "emotional": "Oh, you picked Yuumi? I see how it is, chum.",
    "names": "Kai'Sa, Kha'Zix, Cho'Gath, and Jarvan IV.",
}


def read_pcm(path):
    with wave.open(str(path), "rb") as audio:
        return audio.readframes(audio.getnframes()), audio.getframerate()


def measure(samples, threads, names, options="", save=None, audio=False, worker=None, marker=False, load=None):
    config.KOKORO_NUM_THREADS = threads
    config.KOKORO_CPU_OPTIONS = options
    session.shutdown()
    if worker:
        session.worker_filename = str(Path(worker).resolve())
    cold = time.perf_counter_ns()
    tts.warm_up()
    cold_ms = (time.perf_counter_ns() - cold) / 1e6
    launcher = psutil.Process(session.process.pid)
    # Windows venv python.exe may redirect to a child interpreter. Measure the
    # actual native model process, rather than the small launcher stub.
    process = max([launcher, *launcher.children(recursive=True)], key=lambda item: item.memory_info().rss)
    result = {"threads": threads, "cpu_options": str(options), "worker_cold_ready_ms": cold_ms, "fixtures": {}}
    for name in names:
        collector, first_times, totals, hashes, lengths = VoiceMetrics(True), [], [], [], []
        first_cold = None
        references = []
        active = {model: 0 for model in load.active} if load else {}
        for index in range(samples + 1):
            trace = LatencyTrace(collector if index else VoiceMetrics(True))
            pcm, first, rates = [], [], []
            if load and len(load.active) == 1:
                event = next(iter(load.active.values()))
                if not event.wait(120):
                    raise RuntimeError("Stress model did not enter generation for this sample")
            begin = time.perf_counter_ns()
            def ready(chunk):
                if not first:
                    first.append(time.perf_counter_ns())
                try:
                    data, rate = read_pcm(chunk.path)
                    rates.append(rate)
                    pcm.append(data)
                    if audio:
                        chunk.play()
                finally:
                    chunk.close()
            from voice.playback import playback_session
            with trace_scope(trace), (playback_session() if audio else nullcontext()) as player:
                if marker:
                    from voice.playback import play_samples
                    tone = (np.sin(np.arange(480) * (2 * np.pi * 1000 / 24000)) * 4000).astype("int16").reshape(-1, 1)
                    with trace_scope(None):
                        play_samples(tone, 24000)
                    reference_ns = player.last_end_ns - 20_000_000
                    begin = time.perf_counter_ns()
                    if index:
                        references.append((begin - reference_ns) / 1e6)
                if load and index:
                    for model, event in load.active.items():
                        active[model] += event.is_set()
                tts.stream_speech(FIXTURES[name], ready)
            if not first:
                raise RuntimeError("Kokoro produced no audio")
            elapsed = (first[0] - begin) / 1e6
            if not index:
                first_cold = elapsed
                continue  # Identical one-utterance warm-up for every fixture/setting.
            collector.record("text_to_first_usable_chunk", first[0] - begin)
            first_times.append(elapsed)
            totals.append((time.perf_counter_ns() - begin) / 1e6)
            data = b"".join(pcm)
            hashes.append(hashlib.sha256(data).hexdigest())
            lengths.append(len(data) // 2)
            if save and index == 1:
                save.mkdir(parents=True, exist_ok=True)
                with wave.open(str(save / f"{name}.wav"), "wb") as output:
                    output.setparams((1, 2, rates[0], 0, "NONE", "not compressed"))
                    output.writeframes(data)
        report = collector.report()
        cpu = report.get("native_tts_synthesis_cpu", {}).get("p50_ms")
        wall = report.get("native_tts_synthesis_wall", {}).get("p50_ms")
        memory = process.memory_info()
        result["fixtures"][name] = {
            "first_after_fixture_change_ms": first_cold,
            "metrics": report, "first_audio_variance_ms2": statistics.pvariance(first_times),
            "complete_p50_ms": statistics.median(totals), "complete_max_ms": max(totals),
            "tts_cpu_core_equivalents": cpu / wall if cpu is not None and wall else None,
            "tts_cpu_percent_of_host": cpu / wall / (os.cpu_count() or 1) * 100 if cpu is not None and wall else None,
            "worker_rss_bytes": memory.rss, "worker_peak_working_set_bytes": getattr(memory, "peak_wset", None),
            "unique_pcm_hashes": len(set(hashes)), "first_pcm_hash": hashes[0],
            "sample_count_min_max": [min(lengths), max(lengths)],
            "reference_dac_to_text_request_ms": references, "stress_active_at_request": active,
        }
        print(f"Finished {threads} threads / {name} / {options or 'plain CPU'}", flush=True)
    session.shutdown()
    return result


def silent_playback(samples):
    """Real device timing with zero PCM; never called an audible-output test."""
    import sounddevice as sd
    result = {"kind": "real silent PortAudio output; no non-silent/acoustic measurement", "cases": {}}
    device = sd.query_devices(kind="output")
    result["host_api"] = sd.query_hostapis(device["hostapi"])["name"]
    for block_ms in (0, 5, 10, 20):
        metrics, underflows = VoiceMetrics(True), 0
        for _ in range(samples):
            callback_times, dac_delays, flags = [], [], []
            def callback(output, frames, clock, status):
                output.fill(0)
                callback_times.append(time.perf_counter_ns())
                dac_delays.append(max(0, clock.outputBufferDacTime - clock.currentTime) * 1e9)
                flags.append(bool(status.output_underflow))
            begin = time.perf_counter_ns()
            stream = sd.OutputStream(samplerate=24000, channels=1, dtype="int16", latency="low",
                                     blocksize=0 if not block_ms else int(24 * block_ms), callback=callback)
            opened = time.perf_counter_ns()
            try:
                stream.start()
                started = time.perf_counter_ns()
                time.sleep(.15)
            finally:
                try:
                    stream.stop()
                finally:
                    stream.close()
            metrics.record("stream_open", opened - begin)
            metrics.record("stream_start", started - opened)
            if callback_times:
                metrics.record("open_to_first_callback", callback_times[0] - begin)
                metrics.record("callback_dac_ahead", dac_delays[0])
                for previous, current in zip(callback_times, callback_times[1:]):
                    metrics.record("callback_interval", current - previous)
            underflows += sum(flags)
        result["cases"][str(block_ms)] = {"metrics": metrics.report(), "output_underflows": underflows}
    return result


def compare_playback(samples, baseline_path):
    """Real driver, silent PCM; compare queue-before-start using the same buffer."""
    import importlib.util
    import sounddevice as sd
    from voice.playback import PlaybackSession
    spec = importlib.util.spec_from_file_location("capri_baseline_playback", baseline_path)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    original = sd.OutputStream
    result = {"kind": "real PortAudio, silent PCM; first-buffer/DAC proxy, not audible sound", "cases": {}}
    for name, implementation in (("before", baseline.PlaybackSession), ("after", PlaybackSession),
                                 ("primed", PlaybackSession)):
        metrics, empty_counts, underflow_count, missing_dac = VoiceMetrics(True), [], 0, 0
        for trial in range(samples + 1):
            player = implementation()
            first, empty, underflows = [], [], []
            def factory(**options):
                render = options["callback"]
                def callback(output, frames, clock, status):
                    now = time.perf_counter_ns()
                    underflows.append(bool(status.output_underflow))
                    if not first and player.jobs:
                        # Priming callbacks may have unavailable/zero DAC clocks.
                        # Never turn those zeros into a claimed presentation time.
                        dac = (now + int(max(0, clock.outputBufferDacTime-clock.currentTime) * 1e9)
                               if not status.priming_output and clock.outputBufferDacTime > 0 else None)
                        first.append((now, dac))
                    elif not first:
                        empty.append(True)
                    render(output, frames, clock, status)
                options["callback"] = callback
                if name == "primed":
                    options["prime_output_buffers_using_stream_callback"] = True
                return original(**options)
            sd.OutputStream = factory
            begin = time.perf_counter_ns()
            failed = True
            try:
                player.play(np.zeros((2400, 1), dtype="int16"), 24000, Event(), None)
                failed = False
            finally:
                try:
                    player.close(abort=failed)
                finally:
                    sd.OutputStream = original
            if trial and first:
                metrics.record("pcm_call_to_first_buffer", first[0][0] - begin)
                if first[0][1] is not None:
                    metrics.record("pcm_call_to_first_dac_proxy", first[0][1] - begin)
                else:
                    missing_dac += 1
                empty_counts.append(len(empty))
                underflow_count += sum(underflows)
        result["cases"][name] = {"metrics": metrics.report(), "empty_start_callbacks_total": sum(empty_counts),
                                  "empty_start_callbacks_max": max(empty_counts, default=0),
                                  "output_underflows": underflow_count, "samples_without_dac_clock": missing_dac,
                                  "priming_is_submission_only": name == "primed"}
    return result


def dispatch_bench(samples, minimum, save=None):
    """Synthetic word delivery, real Kokoro and the production chunk pipeline."""
    from core.streaming import StreamingReply
    from core.interaction import InteractionState
    collector, elapsed = VoiceMetrics(True), []
    tts.warm_up()
    for trial in range(samples + 1):
        first, buffers = [], []
        begin = time.perf_counter_ns()
        def consume(chunk):
            data, rate = read_pcm(chunk.path)
            buffers.append(data)
        def prepare(text, ready):
            def delivered(chunk):
                if not first:
                    first.append(time.perf_counter_ns())
                ready(chunk)
            tts.stream_speech(text, delivered, cancel_event=reply.cancellation)
        reply = StreamingReply(Event(), InteractionState(), "A fixture", lambda text: None,
            speak=consume, prepare_stream=prepare, discard=lambda chunk: chunk.close(),
            min_segment_chars=32, first_chunk_max_chars=80, first_sentence_min_chars=minimum)
        try:
            for word in FIXTURES["emotional"].split():
                time.sleep(.04)  # Deliberately synthetic token pacing; not measured Ollama throughput.
                reply.feed(word + " ")
            reply.finish()
        finally:
            reply.abort()
        if not first:
            raise RuntimeError("No usable audio from the production streaming pipeline")
        if trial:
            collector.record("fixture_word_feed_to_first_audio", first[0] - begin)
            elapsed.append((first[0] - begin) / 1e6)
            if save and trial == 1:
                save.mkdir(parents=True, exist_ok=True)
                with wave.open(str(save / "emotional.wav"), "wb") as output:
                    output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    output.writeframes(b"".join(buffers))
    return {"kind": "synthetic 40ms/word feed with real Kokoro; no model, devices or human latency claim",
            "first_sentence_min_chars": minimum, "metrics": collector.report(),
            "first_audio_variance_ms2": statistics.pvariance(elapsed)}


def compare_quality(before, after):
    """Fixed saved speech, objective checks/ASR only; not a listening score."""
    import re
    from voice import voice
    from voice.pronunciation import prepare_kokoro_text
    from benchmark_voice import read_recording
    voice.load_models()
    def tokens(text):
        return re.findall(r"\w+", text.casefold())
    result = {"kind": "one saved waveform per fixture/variant; ASR is a pronunciation proxy, not human listening", "fixtures": {}}
    for name, text in FIXTURES.items():
        paths = (before / f"{name}.wav", after / f"{name}.wav")
        if not all(path.is_file() for path in paths):
            continue
        readings, details = [], []
        for path in paths:
            pcm = read_recording(path)
            readings.append(tokens(voice.transcribe_audio(pcm, Event())))
            details.append({"samples_16k": len(pcm), "rms": float(np.sqrt(np.mean(pcm**2))),
                            "clipped_fraction": float(np.mean(np.abs(pcm) >= 32767/32768)),
                            "non_finite_samples": int(np.sum(~np.isfinite(pcm)))})
        result["fixtures"][name] = {"same_asr_words": readings[0] == readings[1],
            "reference_word_count": len(tokens(prepare_kokoro_text(text))),
            "asr_word_counts": [len(value) for value in readings], "before": details[0], "after": details[1]}
    if not result["fixtures"]:
        raise ValueError("No matching fixed-fixture WAV files in the supplied directories")
    return result


class ModelStress:
    def __init__(self, models=None):
        self.stop = Event()
        self.threads = []
        self.started = {name: Event() for name in (models or ("qwen2.5:32b", config.VISION_MODEL))}
        self.completed = {name: 0 for name in self.started}
        self.active = {name: Event() for name in self.started}

    def __enter__(self):
        import base64, io
        from PIL import Image
        from core.model_client import request_chat
        image = io.BytesIO()
        Image.new("RGB", (640, 360), "#442233").save(image, "JPEG")
        encoded = base64.b64encode(image.getvalue()).decode("ascii")
        def run(model):
            while not self.stop.is_set():
                try:
                    message = {"role": "user", "content": "Explain gravity briefly."}
                    if model == config.VISION_MODEL:
                        message = {"role": "user", "content": "Describe the test image.", "images": [encoded]}
                    def token(text):
                        self.started[model].set()
                        self.active[model].set()
                    request_chat(cancel_event=self.stop, model=model, messages=[message],
                                 on_text=token,
                                 options={"num_predict": 48, **({"num_ctx": 8192} if model == config.VISION_MODEL else {})},
                                 keep_alive=config.MODEL_KEEP_ALIVE)
                    self.completed[model] += 1
                except OperationCancelled:
                    break
                except Exception:
                    if not self.stop.wait(1):
                        continue
                finally:
                    self.active[model].clear()
        for model in self.started:
            thread = Thread(target=run, args=(model,), name="Benchmark model load", daemon=True)
            thread.start()
            self.threads.append(thread)
        for model, event in self.started.items():
            if not event.wait(120):
                self.__exit__()
                raise RuntimeError(f"Stress model {model} did not produce text")
        return self

    def __exit__(self, *args):
        self.stop.set()
        for thread in self.threads:
            thread.join(5)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Stress request did not stop")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--threads", type=int, nargs="+", default=[config.KOKORO_NUM_THREADS])
    parser.add_argument("--fixtures", choices=tuple(FIXTURES), nargs="+", default=["short", "medium", "long", "league", "emotional"])
    parser.add_argument("--cpu-options", type=Path)
    parser.add_argument("--plain-cpu", action="store_true", help="Use the original untuned CPU session")
    parser.add_argument("--worker", type=Path, help="Optional saved baseline worker source")
    parser.add_argument("--save-audio", type=Path, help="Save fixed fixture WAVs for A/B checks, without playing")
    parser.add_argument("--audio", action="store_true", help="Explicitly play fixture speech")
    parser.add_argument("--loopback-marker", action="store_true", help="With --audio, add a 20ms cue before TTS for optional external loopback alignment")
    parser.add_argument("--silent-playback", action="store_true", help="Benchmark real stream startup/buffers using silence only")
    parser.add_argument("--compare-playback", type=Path, help="Compare silent driver startup against a saved playback.py")
    parser.add_argument("--stress", action="store_true", help="Intentionally concurrent Qwen32/vision load; bypasses normal admission")
    parser.add_argument("--stress-models", nargs="+", choices=("qwen2.5:32b", config.VISION_MODEL),
                        help="A single model gates each sample on active generation; default tests concurrent request pressure")
    parser.add_argument("--plain-control", action="store_true", help="Prepend a four-thread plain CPU control under the same load")
    parser.add_argument("--dispatch", action="store_true", help="Compare early complete-sentence dispatch with synthetic word pacing and real TTS")
    parser.add_argument("--quality-compare", type=Path, nargs=2, help="Only compare saved fixture WAV directories using existing Whisper and signal checks")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or any(thread not in (1, 2, 4, 6, 8) for thread in args.threads):
        parser.error("Use 1..100 samples and 1,2,4,6,8 threads")
    if args.loopback_marker and not args.audio:
        parser.error("--loopback-marker requires explicit --audio")
    options = str(args.cpu_options.resolve()) if args.cpu_options else config.KOKORO_CPU_OPTIONS
    if args.plain_cpu or str(options).casefold() == "none":
        options = ""
    import importlib.metadata
    from benchmark_voice import hardware
    result = {"kind": "real installed CPU Kokoro, fixed fixtures; no microphone", "samples": args.samples,
              "speaker": config.KOKORO_SPEAKER_ID, "speed": config.KOKORO_SPEED, "logical_cpus": os.cpu_count(), "runs": []}
    result["hardware"] = hardware()
    result["versions"] = {}
    for name in ("sherpa-onnx", "onnxruntime", "sounddevice"):
        try:
            result["versions"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["versions"][name] = "not installed (optional metadata)"
    import sherpa_onnx
    result["versions"]["sherpa_native_onnxruntime"] = getattr(sherpa_onnx, "onnxruntime_version", "unreported")
    load = ModelStress(args.stress_models) if args.stress else None
    try:
        if load:
            load.__enter__()
        if args.quality_compare:
            result["quality"] = compare_quality(*args.quality_compare)
        if args.plain_control and not args.quality_compare:
            result["runs"].append(measure(args.samples, 4, args.fixtures, "",
                args.save_audio / "control" if args.save_audio else None, args.audio, args.worker, args.loopback_marker, load))
        for threads in ([] if args.quality_compare else args.threads):
            save = args.save_audio / str(threads) if args.save_audio else None
            result["runs"].append(measure(args.samples, threads, args.fixtures,
                options, save, args.audio, args.worker,
                args.loopback_marker, load))
        if load:
            result["stress"] = {"kind": "real concurrent requests; Ollama may queue or swap models", "completed_requests": load.completed}
        if args.silent_playback:
            result["silent_playback"] = silent_playback(args.samples)
        if args.compare_playback:
            result["playback_comparison"] = compare_playback(args.samples, args.compare_playback)
        if args.dispatch:
            result["dispatch"] = {str(value): dispatch_bench(args.samples, value,
                args.save_audio / f"dispatch-{value}" if args.save_audio else None) for value in (0, 20)}
    finally:
        if load:
            load.__exit__()
        session.shutdown()
        from core.model_client import shutdown
        shutdown()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Saved", args.output)


if __name__ == "__main__":
    main()

"""Real output onset A/B test, using Capri's existing playback implementation.

Requires explicit --audio. --loopback passively captures the default Windows
render mix in RAM; keep other audio quiet. No microphone, saved capture, volume
change or monitoring. --reuse-probe tests an existing session across replies
only inside this process, not a new persistent application audio service.
"""
import argparse
import json
import statistics
import time
import wave
from pathlib import Path
from threading import Event

import numpy as np
import sounddevice as sd
import config
from core.latency import VoiceMetrics, LatencyTrace, trace_scope
from voice.playback import PlaybackSession


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    return {"count": len(values), **{f"p{p}_ms": values[int((len(values)-1)*p/100)] for p in (50,95,99)},
            "max_ms": max(values), "variance_ms2": statistics.pvariance(values)}


def paired_interval(before, after):
    """Fixed-seed paired bootstrap interval for median reduction, milliseconds."""
    differences = np.asarray(before) - np.asarray(after)
    rng = np.random.default_rng(417)
    medians = np.median(rng.choice(differences, (2000, len(differences))), axis=1)
    return {"paired_median_reduction_ms": float(np.median(differences)),
            "bootstrap_95_percent_interval_ms": np.quantile(medians, [.025,.975]).tolist(),
            "pairs": len(differences)}


def measure(path, samples, wasapi_device=None, loopback=False, reuse=False, endpoint_id=None):
    from contextlib import nullcontext
    from benchmarks.wasapi_loopback import LoopbackProbe
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("Use a saved PCM16 Kokoro fixture")
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").reshape(-1, source.getnchannels())
        rate = source.getframerate()
    original = sd.OutputStream
    old_wasapi = config.TTS_WASAPI_ENABLED
    config.TTS_WASAPI_ENABLED = False  # Controlled baseline; candidate factory sets WASAPI explicitly.
    names = ["phase3", "wasapi"] if wasapi_device is not None else ["phase3"]
    if reuse:
        names.append("reused_probe")
    traces = {name: VoiceMetrics(True) for name in names}
    onsets = {name: [] for name in names}
    signed = {name: [] for name in names}
    receipts = {name: [] for name in names}
    shared = PlaybackSession() if reuse else None
    try:
        with LoopbackProbe(endpoint_id) if loopback else nullcontext() as probe:
            if probe:
                time.sleep(.25)
            for trial in range(samples + 1):
                # Alternate case order, including the same exact saved PCM.
                for name in (names if trial % 2 else list(reversed(names))):
                    player = shared if name == "reused_probe" else PlaybackSession()
                    def factory(**options):
                        if name == "wasapi":
                            options.update(device=wasapi_device, extra_settings=sd.WasapiSettings(auto_convert=True))
                        return original(**options)
                    sd.OutputStream = factory
                    collector = traces[name] if trial else VoiceMetrics(True)
                    trace = LatencyTrace(collector)
                    if probe:
                        # Quiet guard includes the previous utterance's final mix packets.
                        time.sleep(.25)
                        probe.arm()
                    begin = time.perf_counter_ns()
                    with trace_scope(trace):
                        trace.mark("pcm_ready", begin)
                        try:
                            player.play(pcm, rate, Event(), None)
                        finally:
                            if player is not shared:
                                player.close()
                        if probe:
                            result = probe.result()
                            if result is None:
                                raise RuntimeError("No loopback onset on this endpoint; check route/mute or try --loopback-endpoint")
                            trace.mark("loopback_onset", result[0])
                            trace.mark("loopback_packet_received", result[1])
                            if trial:
                                onsets[name].append((result[0] - begin) / 1e6)
                                receipts[name].append((result[1] - result[0]) / 1e6)
                                if "device_playback" in trace._marks:
                                    signed[name].append((result[0] - trace._marks["device_playback"]) / 1e6)
                    print(f"Playback {name}: sample {trial}/{samples}", flush=True)
            if shared:
                shared.close()
            result = {"kind": "real PCM output; DAC estimate / optional digital WASAPI mix, not acoustic",
                      "loopback_format": probe.format if probe else None,
                      "loopback_invalid_timestamps": probe.invalid_timestamps if probe else None,
                      "loopback_discontinuities": probe.discontinuities if probe else None,
                      "cases": {name: {"metrics": traces[name].report(), "loopback_onset": distribution(onsets[name]),
                                       "signed_packet_receipt_minus_loopback_qpc": distribution(receipts[name]),
                                       "signed_loopback_minus_dac": distribution(signed[name])} for name in names}}
            if wasapi_device is not None and probe:
                result["paired_loopback_reduction"] = paired_interval(onsets["phase3"], onsets["wasapi"])
            return result
    finally:
        try:
            if shared:
                shared.close()
        finally:
            sd.OutputStream = original
            config.TTS_WASAPI_ENABLED = old_wasapi


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--loopback", action="store_true")
    parser.add_argument("--loopback-endpoint", help="Optional Windows render endpoint ID; defaults to the current system output")
    parser.add_argument("--reuse-probe", action="store_true")
    parser.add_argument("--wasapi-device", type=int)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.audio or not 1 <= args.samples <= 100:
        parser.error("Playback requires --audio and 1..100 samples")
    result = measure(args.recording, args.samples, args.wasapi_device, args.loopback, args.reuse_probe, args.loopback_endpoint)
    result["devices"] = {"default": dict(sd.query_devices(kind="output")),
                         "candidate": dict(sd.query_devices(args.wasapi_device)) if args.wasapi_device is not None else None}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print("Saved",args.output)


if __name__ == "__main__":
    main()

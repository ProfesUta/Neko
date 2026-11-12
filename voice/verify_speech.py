"""Explicit speech-integrity replay, never enabled in normal Capri operation.

Uses the installed voice, existing StreamingReply, native chunks and playback.
--audio plays fixtures. --loopback additionally saves bounded Windows render-mix
captures for listening; keep other output quiet. No microphone or LLM is used.
Personal fixture text/ASR is saved only with --trace, inside ignored artifacts/.
"""
import argparse
import json
import shutil
import time
import wave
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Event, Lock

import numpy as np

import config
from core.interaction import InteractionState
from core.latency import LatencyTrace, VoiceMetrics, trace_scope
from core.streaming import StreamingReply
from voice import tts
from voice.kokoro_session import session
from voice.personal_pronunciation import compile_profile, read_profile
from voice.kokoro_worker import CHAMPION_LEXICON
from voice.playback import PlaybackSession
from voice.pronunciation import prepare_kokoro_text


def fixtures():
    profile = config.PERSONAL_PRONUNCIATIONS_FILE
    name = read_profile(profile)[0]["display_name"] if profile and Path(profile).is_file() else "DemoPerson"
    return ["What's on your mind?", "Hello! What's on your mind?",
            f"{name}, what's on your mind?", f"Hello, {name}. How are you?",
            "What are you doing?", "Where are you going?", "I wanted to ask you something.",
            "Today is a beautiful day.", "I'm here if you need anything.",
            "Can you hear every word I'm saying?", "What's?", "I'm?", "You're?", "Don't?", "Can't?", "It's?",
            "What's on your mind? I wanted to ask you something. I'm here if you need anything. Can you hear every word I'm saying?"]


def read_pcm(path):
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("Expected Kokoro PCM16")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").reshape(-1, source.getnchannels()), source.getframerate()


def save_pcm(path, pcm, rate):
    with wave.open(str(path), "wb") as target:
        target.setparams((pcm.shape[1], 2, rate, 0, "NONE", "not compressed"))
        target.writeframes(np.asarray(pcm, dtype="<i2").tobytes())


class MixCapture:
    """Opt-in, <=15 seconds per fixture, no file I/O on the capture thread."""
    def __init__(self):
        self.lock = Lock()
        self.begin = None
        self.packets = []
        self.frames = 0
        self.bytes = 0
        self.format = None
        self.incomplete = False

    def arm(self, begin):
        with self.lock:
            self.begin, self.packets, self.frames, self.bytes = begin, [], 0, 0
            self.incomplete = False

    def __call__(self, raw, frames, at, flags, fmt):
        with self.lock:
            if self.begin is None or at < self.begin:
                return
            if (self.frames + frames <= fmt["rate"] * 15 and self.bytes + len(raw) <= 16 * 1024 * 1024):
                self.packets.append(raw)
                self.frames += frames
                self.bytes += len(raw)
                self.format = fmt
            else:
                self.incomplete = True
            self.incomplete |= bool(flags & 5)  # Discontinuous PCM / invalid timestamps.

    def save(self, path):
        with self.lock:
            self.begin = None
            data, fmt = b"".join(self.packets), self.format
            if self.incomplete:
                raise RuntimeError("Incomplete diagnostic loopback capture; discard this sample and retry")
        if not data or fmt is None:
            raise RuntimeError("No loopback PCM; check the selected output route")
        dtype = "<f4" if fmt["floating"] else f'<i{fmt["bits"]//8}'
        pcm = np.frombuffer(data, dtype=dtype).reshape(-1, fmt["channels"])
        if fmt["floating"]:
            pcm = np.clip(pcm, -1, 1) * 32767
        elif fmt["bits"] == 32:
            pcm = pcm.astype("float64") / 65536
        save_pcm(path, pcm, fmt["rate"])


@contextmanager
def inspect_output(enabled):
    """Verify actual callback PCM, including first/last frames, off callback."""
    original = PlaybackSession._callback
    rendered, spans = [], []
    if enabled:
        def callback(player, output, frames, clock, status):
            before = [(job, job.offset) for job in list(player.jobs)]
            original(player, output, frames, clock, status)
            rendered.append(output.copy())
            position = 0
            for job, offset in before:
                count = job.offset - offset
                if count:
                    spans.append((job, offset, job.offset, output[position:position+count].copy()))
                    position += count
        PlaybackSession._callback = callback
    state = {"rendered": rendered, "spans": spans}
    try:
        yield state
    finally:
        PlaybackSession._callback = original


def check_spans(spans):
    progress = {}
    for job, start, end, pcm in spans:
        assert start == progress.get(id(job), 0), "Device skipped/repeated a PCM range"
        assert np.array_equal(pcm, job.samples[start:end]), "Callback PCM differs from source"
        progress[id(job)] = end
    for job, *_ in spans:
        assert progress[id(job)] == len(job.samples), "Device omitted final samples"
    return len(progress)


def replay(text, mode, output, audio=False, probe=None, capture=None):
    output.mkdir(parents=True, exist_ok=True)
    collector = VoiceMetrics(True)
    trace = LatencyTrace(collector)
    segments, delivered, played, private = [], [], [], []
    personal = config.PERSONAL_PRONUNCIATIONS_FILE
    profile = compile_profile(personal, config.KOKORO_VOICE_DIR, CHAMPION_LEXICON) if personal and Path(personal).is_file() else None
    def inspect_text(sentence):
        clean = tts.clean_for_speech(sentence)
        prepared = prepare_kokoro_text(clean)
        private.append({"segment": sentence, "cleaned": clean, "prepared": prepared,
                        "native": profile.apply(prepared) if profile else prepared})
        segments.append(sentence)
        return prepared
    def delivered_path(path):
        target = output / f"chunk-{len(delivered):02d}.wav"
        shutil.copyfile(path, target)
        delivered.append(target)
    def speak(chunk):
        played.append(chunk.path.name)
        if audio:
            chunk.play()
    def prepare(sentence, ready):
        inspect_text(sentence)
        def native_ready(chunk):
            delivered_path(chunk.path)
            ready(chunk)
        tts.stream_speech(sentence, native_ready, Event())
    if probe:
        time.sleep(.25)
        probe.arm()
    begin = time.perf_counter_ns()
    if capture:
        capture.arm(begin)
    session._diagnostics.clear()
    with inspect_output(audio) as output_check, trace_scope(trace):
        if mode == "conversation":
            display = []
            reply = StreamingReply(Event(), InteractionState(), "Explain", display.append,
                speak=speak, prepare_stream=prepare, discard=lambda chunk: chunk.close(),
                min_segment_chars=config.TTS_STREAM_MIN_CHARS,
                first_chunk_max_chars=config.TTS_FIRST_CHUNK_MAX_CHARS,
                first_sentence_min_chars=config.TTS_FIRST_SENTENCE_MIN_CHARS)
            try:
                for offset in range(0, len(text), 3):
                    reply.feed(text[offset:offset+3])
                reply.finish()
                assert reply.text == text and display[-1] == text
            finally:
                reply.abort()
            assert len(played) == len(delivered), "Prefetch dropped a native chunk"
        else:
            prepared = inspect_text(text)
            path = output / "native.wav"
            def ready(message):
                trace.mark("audio_ready")
                delivered_path(message["chunk"])
                Path(message["chunk"]).unlink()
            trace.mark("tts_submitted")
            session.request(config.KOKORO_VOICE_DIR, text=prepared, output=path,
                            on_chunk=ready if mode == "native" else None, **tts.kokoro_settings())
            if mode == "full":
                trace.mark("audio_ready")
                delivered_path(path)
                path.unlink()
            if audio:
                from voice.playback import playback_session
                with playback_session():
                    for chunk in delivered:
                        tts._play_wav(chunk, None)
        finished = time.perf_counter_ns()
        onset = probe.result() if probe else None
        if probe and onset is None:
            raise RuntimeError("No digital loopback onset; check output route/mute")
    pcms = [read_pcm(path) for path in delivered]
    assert pcms and len({rate for _, rate in pcms}) == 1
    pcm = np.concatenate([values for values, _ in pcms])
    rate = pcms[0][1]
    save_pcm(output / "generated.wav", pcm, rate)
    if audio:
        assert check_spans(output_check["spans"]) == len(delivered), "Missing/extra device jobs"
        save_pcm(output / "submitted.wav", np.concatenate(output_check["rendered"]), rate)
    if capture:
        time.sleep(.12)  # Diagnostic capture tail AFTER playback; never delays production speech.
        capture.save(output / "loopback.wav")
    signal = np.flatnonzero(np.max(np.abs(pcm.astype("int32")), axis=1) > 32)
    result = {"mode": mode, "chunks": len(delivered), "frames": len(pcm), "rate": rate,
              "leading_frames": int(signal[0]) if signal.size else None,
              "ready_ms": (trace._marks["audio_ready"] - begin)/1e6,
              "completion_ms": (finished-begin)/1e6,
              "digital_onset_ms": (onset[0]-begin)/1e6 if onset else None,
              "oov_messages": sum("OOV word skipped" in line for line in session._diagnostics),
              "callback_pcm_exact": True if audio else None, "metrics": collector.report()}
    return result, {"original": text, "display": text, "stages": private, "native_log": list(session._diagnostics)}


def run(output, samples=20, audio=False, loopback=False, trace=False, recognize=False, endpoint=None,
        modes=("full", "native", "conversation"), selected=None, loopback_threshold=.001):
    from benchmarks.wasapi_loopback import LoopbackProbe
    from benchmark_playback import distribution
    output.mkdir(parents=True, exist_ok=True)
    capture = MixCapture() if loopback else None
    results, private = [], []
    try:
        with LoopbackProbe(endpoint, capture, threshold=loopback_threshold) if loopback else nullcontext() as probe:
            texts = fixtures()
            selected = list(range(len(texts))) if selected is None else selected
            for mode in modes:
                session.shutdown()
                # Each fixture gets initial and consecutive playback. Only the
                # very first request per mode includes model loading.
                for trial in range(2):
                    for index in selected:
                        text = texts[index]
                        folder = f"{mode}-{trial}-{index:02d}"
                        item, stages = replay(text, mode, output / folder, audio, probe, capture)
                        item.update(fixture=index, trial=trial, folder=folder, cold_engine=trial==0 and index==0)
                        results.append(item)
                        private.append({"folder": folder, **stages})
                        print(f"{mode}: pass {trial}, fixture {index}, OOV={item['oov_messages']}", flush=True)
                # Two confirmed missing-word cases, twenty warm responses each.
                for index in (value for value in (0, 6) if value in selected):
                    for trial in range(samples):
                        folder = f"warm-{mode}-{index:02d}-{trial:02d}"
                        item, stages = replay(texts[index], mode, output / folder, audio, probe, capture)
                        item.update(fixture=index, trial=trial, folder=folder, cold_engine=False)
                        results.append(item)
                        if trace:
                            private.append({"folder": folder, **stages})
                        if trial % 5 == 0:
                            print(f"{mode}: fixture {index}, warm sample {trial+1}/{samples}", flush=True)
    finally:
        session.shutdown()
    summary = {"measurement": "Real Kokoro; optional actual device callback/digital mix, never acoustic output",
               "settings": {**tts.kokoro_settings(), "threads": config.KOKORO_NUM_THREADS,
                            "wasapi": config.TTS_WASAPI_ENABLED}, "cases": results}
    if probe:
        summary["loopback"] = {"threshold": loopback_threshold, "format": probe.format,
                               "invalid_timestamps": probe.invalid_timestamps, "discontinuities": probe.discontinuities}
    summary["warm"] = {f"{mode}-{index}": {metric: distribution([item[metric] for item in results
        if item["folder"].startswith("warm-") and item["mode"]==mode and item["fixture"]==index and item[metric] is not None])
        for metric in ("ready_ms", "completion_ms", "digital_onset_ms")}
        for mode in ("full", "native", "conversation") for index in (0, 6)}
    if trace:
        (output / "private-trace.json").write_text(json.dumps(private, indent=2, ensure_ascii=False), encoding="utf-8")
    if recognize:
        from faster_whisper import WhisperModel
        model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
        observations = []
        for item in results:
            if item["folder"].startswith("warm-"):
                continue
            for kind in ("generated", "loopback") if loopback else ("generated",):
                segments, _ = model.transcribe(str(output/item["folder"]/f"{kind}.wav"), language="en", beam_size=1,
                                              vad_filter=False, condition_on_previous_text=False)
                observations.append({"folder": item["folder"], "audio": kind, "recognized": " ".join(s.text.strip() for s in segments)})
        # Always private: recognizer may reproduce the personal name.
        if trace:
            (output / "private-asr.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/speech-integrity/verification"))
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--loopback", action="store_true")
    parser.add_argument("--loopback-endpoint")
    parser.add_argument("--loopback-threshold", type=float, default=.001,
                        help="Diagnostic amplitude threshold; calibrate against quiet output, never changes playback")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--recognize", action="store_true", help="Optional local ASR observations, not proof of correct speech")
    parser.add_argument("--fixtures", type=int, nargs="+", help="Fixture numbers 0..16; 16 exercises multiple native chunks")
    parser.add_argument("--modes", nargs="+", choices=("full", "native", "conversation"), default=("full", "native", "conversation"))
    args = parser.parse_args()
    if not 0 <= args.samples <= 100 or args.loopback and not args.audio:
        parser.error("Use 0..100 warm samples; --loopback requires --audio")
    if not 0 < args.loopback_threshold < 1:
        parser.error("Loopback threshold must be between zero and one")
    if args.fixtures and any(not 0 <= index <= 16 for index in args.fixtures):
        parser.error("Fixture numbers must be 0..16")
    run(args.output, args.samples, args.audio, args.loopback, args.trace, args.recognize,
        args.loopback_endpoint, args.modes, args.fixtures, args.loopback_threshold)
    print("Saved numeric summary and diagnostic WAVs. Confirm speech by listening; ASR and waveform activity are not proof.")


if __name__ == "__main__":
    main()

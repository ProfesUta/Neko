"""Privacy-safe voice benchmarks; default uses fixtures, never a microphone.

Optional --tts runs the installed voice without playback. --ollama measures
fixed prompts on the selected local model. --contention also sends a generated
test image (never the desktop) to the configured vision model. --audio explicitly
permits speaker playback; device timestamps are not acoustic measurements.
"""

import argparse
import json
import platform
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event, Timer
from concurrent.futures import ThreadPoolExecutor

import httpx

import config
from core.cancellation import OperationCancelled
from core.interaction import InteractionState
from core.league_metrics import LeagueMetrics
from core.streaming import StreamingReply

FIXTURE = ("Keep your answer grounded in what we know, and explain the useful part first, "
           "so we can work through the details together. Then ask if anything is unclear.")
QUESTION = "In two brief sentences, explain why the sky looks blue."


def timed(metrics, name, call):
    start = time.perf_counter_ns()
    result = call()
    metrics.record(name, time.perf_counter_ns() - start)
    return result


def synthetic(samples):
    metrics = LeagueMetrics(True)
    high_water = 0
    for _ in range(samples):
        begin = time.perf_counter_ns()
        first = Event()
        def prepare(text):
            time.sleep(.02)  # Fixed simulated synthesis cost, not a voice measurement.
            return text
        def play(text):
            if not first.is_set():
                metrics.record("fixture_tokens_to_first_audio_ready", time.perf_counter_ns() - begin)
                first.set()
        options = {"min_segment_chars": config.TTS_STREAM_MIN_CHARS}
        # Read the production setting when adaptive chunking is available.
        if hasattr(config, "TTS_FIRST_CHUNK_MAX_CHARS"):
            options["first_chunk_max_chars"] = config.TTS_FIRST_CHUNK_MAX_CHARS
        reply = StreamingReply(Event(), InteractionState(), "Explain", lambda text: None,
                               play, prepare=prepare, **options)
        try:
            for word in FIXTURE.split():
                time.sleep(.008)  # Fixed synthetic token delivery, identical before/after.
                reply.feed(word + " ")
                high_water = max(high_water, reply._queue.qsize())
            reply.finish()
        finally:
            reply.abort()
        metrics.record("fixture_reply_complete", time.perf_counter_ns() - begin)
    # Cancellation uses the real model adapter with a stalled fake transport.
    import asyncio
    import ollama
    from core import model_client
    real_client = model_client.ollama.AsyncClient
    async def blocked(request):
        await asyncio.sleep(30)
    model_client.ollama.AsyncClient = lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(blocked))
    try:
        for _ in range(samples):
            stop, stopped_at = Event(), []
            def interrupt():
                stopped_at.append(time.perf_counter_ns())
                stop.set()
            timer = Timer(.02, interrupt)
            timer.start()
            try:
                model_client.request_chat(cancel_event=stop, model="fixture", messages=[])
            except OperationCancelled:
                metrics.record("stalled_http_cancellation", time.perf_counter_ns() - stopped_at[0])
            finally:
                timer.cancel()
                timer.join()
    finally:
        shutdown = getattr(model_client, "shutdown", None)
        if shutdown:
            shutdown()
        model_client.ollama.AsyncClient = real_client
    return {"kind": "synthetic token feed, synthesis and stalled HTTP; no microphone or speakers",
            "token_word_delay_ms": 8, "simulated_synthesis_ms": 20,
            "text_queue_high_water": high_water, "metrics": metrics.report()}


def tts_bench(samples, audio=False):
    from voice import tts
    from voice.kokoro_session import session
    metrics = LeagueMetrics(True)
    session.shutdown()
    try:
        timed(metrics, "voice_cold_load", tts.warm_up)
        for _ in range(samples):
            prepared = timed(metrics, "warm_tts_audio_ready", lambda: tts.prepare_speech("Hey, chum. I'm right here."))
            with prepared:
                if audio:
                    begin = time.perf_counter_ns()
                    prepared._on_playback_start = lambda: metrics.record("audio_ready_to_playback_callback", time.perf_counter_ns() - begin)
                    prepared.play()
        if hasattr(tts, "stream_speech"):
            phrase = FIXTURE.split(" Then", 1)[0]
            for _ in range(samples):
                begin, first = time.perf_counter_ns(), []
                def ready(chunk):
                    if not first:
                        first.append(True)
                        metrics.record("native_stream_first_audio_ready", time.perf_counter_ns() - begin)
                    chunk.close()
                tts.stream_speech(phrase, ready)
                metrics.record("native_stream_complete", time.perf_counter_ns() - begin)
        return {"kind": "real installed Kokoro CPU synthesis; " + ("speaker output enabled" if audio else "no playback"),
                "speaker": config.KOKORO_SPEAKER_ID, "speed": config.KOKORO_SPEED,
                "threads": getattr(config, "KOKORO_NUM_THREADS", 2), "metrics": metrics.report()}
    finally:
        session.shutdown()


def resident_models():
    with httpx.Client(timeout=2, trust_env=False) as http:
        response = http.get(config.OLLAMA_HOST + "/api/ps")
        response.raise_for_status()
        return [{key: model.get(key) for key in ("name", "size", "size_vram", "context_length")}
                for model in response.json().get("models", [])]


def ollama_bench(samples, model):
    from core.model_client import request_chat
    from core.conversation import conversation_model
    selected = conversation_model() if model == "auto" else model
    metrics = LeagueMetrics(True)
    from core.latency import VoiceMetrics
    rates = VoiceMetrics(True)
    residency = resident_models()
    for index in range(samples):
        start = time.perf_counter_ns()
        first = []
        def token(text):
            if not first:
                first.append(time.perf_counter_ns())
                metrics.record("first_request_ttft" if index == 0 else "subsequent_request_ttft", first[0] - start)
        response = request_chat(model=selected, messages=[
            {"role": "system", "content": config.SYSTEM_PROMPT.split("\nTOOLS:", 1)[0]},
            {"role": "user", "content": QUESTION}], on_text=token, keep_alive=config.MODEL_KEEP_ALIVE,
            **({"think": False} if selected.startswith("qwen3:") else {}),
            options={"temperature": 0, "num_predict": 48})
        metrics.record("request_complete", time.perf_counter_ns() - start)
        # Prefer server token counts/durations; never count string fragments as tokens.
        count, duration = getattr(response, "eval_count", None), getattr(response, "eval_duration", None)
        if count and duration:
            metrics.record("server_ns_per_generated_token", duration / count)
            rates.record_tokens(count, duration)
        for name in ("load_duration", "prompt_eval_duration", "eval_duration"):
            value = getattr(response, name, None)
            if isinstance(value, int):
                metrics.record("server_" + name, value)
    return {"kind": "real local Ollama; fixed synthetic question; no audio", "model": selected,
            "resident_before": residency, "resident_after": resident_models(), "metrics": {**metrics.report(), **rates.report()},
            "limits": "First request is cold only if the selected model was not already resident; no forced unload."}


def hardware():
    result = {"logical_cpu_count": __import__("os").cpu_count()}
    try:
        output = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
        if output.returncode == 0:
            result["gpu_name_total_used_mib_util_percent"] = output.stdout.strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        result["gpu_monitor"] = "unavailable"
    return result


def contention_bench(model):
    import base64
    import io
    from PIL import Image, ImageDraw
    from core.conversation import conversation_model
    from core.model_client import request_chat
    from core.inference_priority import priority
    selected = conversation_model() if model == "auto" else model
    image = Image.new("RGB", (640, 360), "#202020")
    ImageDraw.Draw(image).text((30, 30), "Fixture: two blue circles and a red square", fill="white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    def text():
        begin, first = time.perf_counter_ns(), []
        request_chat(model=selected, messages=[{"role": "user", "content": "Explain gravity in one short sentence."}],
                     on_text=lambda value: first.append((time.perf_counter_ns() - begin) / 1e6) if not first else None,
                     **({"think": False} if selected.startswith("qwen3:") else {}),
                     options={"num_predict": 32}, keep_alive=config.MODEL_KEEP_ALIVE)
        return {"ttft_ms": first[0] if first else None, "wall_ms": (time.perf_counter_ns() - begin) / 1e6}
    def vision():
        begin = time.perf_counter_ns()
        try:
            request_chat(model=config.VISION_MODEL,
                         messages=[{"role": "user", "content": "Describe the test image in one short sentence.", "images": [encoded]}],
                         options={"num_ctx": 8192, "num_predict": 32}, keep_alive=config.MODEL_KEEP_ALIVE, background=True)
            return {"wall_ms": (time.perf_counter_ns() - begin) / 1e6, "preempted": False}
        except OperationCancelled:
            return {"wall_ms": (time.perf_counter_ns() - begin) / 1e6, "preempted": True}
    result = {"kind": "real local models; generated test image, no desktop or audio", "text_model": selected,
              "text_only": [text() for _ in range(3)]}
    class User:
        pass
    owner = User()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(vision)
        time.sleep(.5)
        priority.user_started(owner)
        try:
            result["concurrent_text"] = [text() for _ in range(3)]
        finally:
            priority.user_finished(owner)
        result["vision"] = future.result()
    result["resident_after"] = resident_models()
    return result


def replay_bench(samples, model):
    """Real VAD/STT/model/TTS, replayed synthetic speech, no input/output devices."""
    import numpy as np
    import torch
    import wave
    from voice import voice, tts
    from voice.kokoro_session import session
    from core import conversation
    from core.context import Context
    from gui.workers import ChatWorker
    from memory import database
    metrics = LeagueMetrics(True)
    old_model = config.CONVERSATION_MODEL
    old_database = database.DATABASE
    old_play, old_chat = tts._play_wav, conversation.request_chat
    config.CONVERSATION_MODEL = model
    def seeded_chat(**kwargs):
        kwargs["options"] = {**kwargs.get("options", {}), "seed": 42}
        return old_chat(**kwargs)
    conversation.request_chat = seeded_chat
    try:
        voice.load_models()
        tts.warm_up()
        old_chat(model=conversation.conversation_model(), messages=[], keep_alive=config.MODEL_KEEP_ALIVE)
        with tempfile.TemporaryDirectory(prefix="capri-voice-fixture-") as directory:
            database.DATABASE = Path(directory) / "memory.db"  # Never read personal facts in a benchmark.
            path = Path(directory) / "fixture.wav"
            tts._synthesize("Explain gravity in one short sentence.", path, Event())
            with wave.open(str(path), "rb") as source:
                pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768
                rate = source.getframerate()
            pcm = np.interp(np.arange(int(len(pcm) * 16000 / rate)) * rate / 16000, np.arange(len(pcm)), pcm).astype(np.float32)
            signal = np.concatenate((np.zeros(3200, dtype=np.float32), pcm, np.zeros(16000, dtype=np.float32)))
            last_sound = int(np.flatnonzero(np.abs(signal) > 32 / 32768)[-1])
            for _ in range(samples):
                trace = None
                if getattr(config, "VOICE_LATENCY_METRICS_ENABLED", False):
                    from core.latency import start_trace
                    trace = start_trace()
                voice.vad_iterator.reset_states()
                replay_start = time.perf_counter_ns()
                for end in range(512, len(signal), 512):
                    deadline = replay_start + int(end * 1e9 / 16000)
                    remaining = (deadline - time.perf_counter_ns()) / 1e9
                    if remaining > 0:
                        time.sleep(remaining)
                    detected = voice.vad_iterator(torch.from_numpy(signal[end - 512:end]), return_seconds=False)
                    if detected and "end" in detected:
                        break
                else:
                    raise RuntimeError("VAD did not endpoint the synthesized fixture")
                speech_end = replay_start + int(last_sound * 1e9 / 16000)
                if trace is not None:
                    trace.mark("speech_end_estimate", speech_end)
                    trace.mark("vad_detected")
                metrics.record("fixture_speech_end_to_vad", time.perf_counter_ns() - speech_end)
                question = timed(metrics, "fixture_stt", lambda: voice.transcribe_audio(signal[:end], Event()))
                first = []
                def ready(path, cancellation, on_playback_start=None):
                    check = time.perf_counter_ns()
                    if not first:
                        first.append(True)
                        metrics.record("fixture_speech_end_to_first_audio_ready", check - speech_end)
                    # No device or fake playback-start callback: this measures readiness only.
                tts._play_wav = ready
                worker = ChatWorker([{"role": "system", "content": config.SYSTEM_PROMPT}], Context(), question, True)
                errors = []
                worker.error.connect(lambda error: errors.append(type(error).__name__))
                worker.run()
                if errors or not first:
                    raise RuntimeError("Replayed fixture did not reach audio readiness")
        return {"kind": "real VAD/STT/Ollama/Kokoro on paced synthesized speech; no microphone or speaker",
                "model": conversation.conversation_model(), "metrics": metrics.report(),
                "limits": "Fixture speech end uses a PCM amplitude threshold, not a human/acoustic measurement."}
    finally:
        config.CONVERSATION_MODEL = old_model
        database.DATABASE = old_database
        tts._play_wav, conversation.request_chat = old_play, old_chat
        session.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--tts", action="store_true")
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--ollama", action="store_true")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--contention", action="store_true")
    parser.add_argument("--replay", action="store_true", help="Paced synthesized speech through real VAD/STT/model/TTS; no devices")
    parser.add_argument("--profile", action="store_true", help="Include production numeric stage and native CPU metrics")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("--samples must be 1..100")
    if args.profile:
        config.VOICE_LATENCY_METRICS_ENABLED = True
    result = {"python": platform.python_version(), "hardware": hardware(), "synthetic": synthetic(args.samples),
              "settings": {"conversation_model": config.CONVERSATION_MODEL, "primary_model": config.MODEL,
                           "voice_speaker": config.KOKORO_SPEAKER_ID, "voice_speed": config.KOKORO_SPEED,
                           "tts_threads": getattr(config, "KOKORO_NUM_THREADS", 2),
                           "whisper_device": getattr(config, "WHISPER_DEVICE", "auto"),
                           "native_tts_chunks": getattr(config, "TTS_NATIVE_STREAMING_ENABLED", False),
                           "silence_ms": config.VOICE_SILENCE_MS},
              "limits": ["No human speech-end/acoustic latency without a microphone/loopback measurement.",
                         "Synthetic token timing is not measured Ollama throughput."]}
    if args.tts or args.audio:
        result["tts"] = tts_bench(args.samples, args.audio)
    if args.ollama:
        result["ollama"] = ollama_bench(args.samples, args.model)
    if args.contention:
        result["contention"] = contention_bench(args.model)
    if args.replay:
        result["replay"] = replay_bench(args.samples, args.model)
    if args.profile:
        from core.latency import metrics
        result["production_stage_metrics"] = metrics.report()
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()

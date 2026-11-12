"""Privacy-safe voice benchmarks; default uses fixtures, never a microphone.

Optional --tts runs the installed voice without playback. --ollama measures
fixed prompts on the selected local model. --contention also sends a generated
test image (never the desktop) to the configured vision model. --audio explicitly
permits speaker playback; device timestamps are not acoustic measurements.
"""

import argparse
import json
import platform
import re
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


def ollama_bench(samples, model, question=QUESTION, first_text_only=False):
    from core.model_client import request_chat
    from core.conversation import conversation_model
    selected = conversation_model() if model == "auto" else model
    metrics = LeagueMetrics(True)
    from core.latency import VoiceMetrics
    rates = VoiceMetrics(True)
    residency = resident_models()
    prompt_tokens = None
    for index in range(samples + 1):
        start = time.perf_counter_ns()
        first = []
        def token(text):
            if not first and text.strip():
                first.append(time.perf_counter_ns())
                metrics.record("first_request_ttft" if index == 0 else "subsequent_request_ttft", first[0] - start)
                if first_text_only:
                    raise OperationCancelled()
        try:
            response = request_chat(model=selected, messages=[
                {"role": "system", "content": config.SYSTEM_PROMPT.split("\nTOOLS:", 1)[0]},
                {"role": "user", "content": question}], on_text=token, keep_alive=config.MODEL_KEEP_ALIVE,
                **({"think": False} if selected.startswith("qwen3:") else {}),
                options={"temperature": 0, "num_predict": 48})
        except OperationCancelled:
            if not first_text_only or not first:
                raise
            continue
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
        if isinstance(getattr(response, "prompt_eval_count", None), int):
            prompt_tokens = response.prompt_eval_count
    return {"kind": "real local Ollama; fixed synthetic question; no audio", "model": selected,
            "resident_before": residency, "resident_after": resident_models(), "metrics": {**metrics.report(), **rates.report()},
            "last_prompt_tokens": prompt_tokens, "first_text_only": first_text_only,
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


def contention_bench(model, samples=20, followups=False):
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
    admitted = Event()
    vision_stop = Event()
    def vision():
        begin = time.perf_counter_ns()
        try:
            with priority.background(vision_stop) as cancellation:
                admitted.set()
                request_chat(cancel_event=cancellation, model=config.VISION_MODEL,
                             messages=[{"role": "user", "content": "Describe the test image in one short sentence.", "images": [encoded]}],
                             options={"num_ctx": 8192, "num_predict": 32}, keep_alive=config.MODEL_KEEP_ALIVE)
            return {"wall_ms": (time.perf_counter_ns() - begin) / 1e6, "preempted": False}
        except OperationCancelled:
            return {"wall_ms": (time.perf_counter_ns() - begin) / 1e6, "preempted": True}
    result = {"kind": "real local models; generated test image, no desktop or audio", "text_model": selected,
              "text_only": [text() for _ in range(samples)]}
    class User:
        pass
    owner = User()
    if followups:
        previous = config.INFERENCE_BACKGROUND_QUIET_SECONDS
        result = {"kind": "real local GPU/model TTFT after a paced three-second follow-up gap; no human session",
                  "idle_gap_seconds": 3, "cases": {}}
        try:
            with ThreadPoolExecutor(1) as pool:
                for quiet in (2, 6):
                    config.INFERENCE_BACKGROUND_QUIET_SECONDS = quiet
                    metrics, admissions = LeagueMetrics(True), 0
                    for _ in range(samples):
                        priority.user_started(owner)
                        priority.user_finished(owner)
                        admitted.clear()
                        vision_stop = Event()
                        future = pool.submit(vision)
                        time.sleep(3)  # Actual elapsed idle interval, identical in both policies.
                        admissions += admitted.is_set()
                        priority.user_started(owner)
                        vision_stop.set()  # Same cancellation as foreground screen pause.
                        try:
                            trial = text()
                            if trial["ttft_ms"] is not None:
                                metrics.record("followup_ttft", trial["ttft_ms"] * 1e6)
                        finally:
                            priority.user_finished(owner)
                        future.result()
                    result["cases"][str(quiet)] = {"background_admissions": admissions, "metrics": metrics.report()}
            return result
        finally:
            config.INFERENCE_BACKGROUND_QUIET_SECONDS = previous
            priority.user_finished(owner)
    result["concurrent_text"], result["vision"] = [], []
    with ThreadPoolExecutor(1) as pool:
        for _ in range(samples):
            # Each trial admits real background inference before user priority.
            admitted.clear()
            vision_stop = Event()
            future = pool.submit(vision)
            if not admitted.wait(10):
                raise RuntimeError("Background admission did not become available")
            time.sleep(.5)
            priority.user_started(owner)
            try:
                result["concurrent_text"].append(text())
            finally:
                priority.user_finished(owner)
            result["vision"].append(future.result())
    summary = LeagueMetrics(True)
    for name in ("text_only", "concurrent_text"):
        for trial in result[name]:
            if trial["ttft_ms"] is not None:
                summary.record(name + "_ttft", trial["ttft_ms"] * 1e6)
    result["metrics"] = summary.report()
    result["resident_after"] = resident_models()
    return result


def read_recording(path):
    """Explicit WAV fixture only; never opens an input device or logs audio."""
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("Use an uncompressed 16-bit PCM WAV recording")
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768
        pcm = pcm.reshape(-1, source.getnchannels()).mean(axis=1)
        rate = source.getframerate()
    if not len(pcm) or len(pcm) / rate > 60:
        raise ValueError("Recording must contain 0..60 seconds of speech")
    return np.interp(np.arange(int(len(pcm) * 16000 / rate)) * rate / 16000,
                     np.arange(len(pcm)), pcm).astype(np.float32)


def interruption_bench(samples, drain_ms=None, audio=False):
    """Real synthesis cancellation; no device output or acoustic stop claim."""
    from voice import tts
    from voice.kokoro_session import session
    previous = getattr(config, "TTS_CANCEL_DRAIN_SECONDS", 0)
    if drain_ms is not None:
        config.TTS_CANCEL_DRAIN_SECONDS = drain_ms / 1000
    result = {"kind": "real Kokoro; cancellation injected after first chunk; no playback",
              "drain_ms": getattr(config, "TTS_CANCEL_DRAIN_SECONDS", 0) * 1000, "cases": {}}
    try:
        tts.warm_up()
        for name, phrase in (("single_chunk", "Gravity pulls masses together."), ("continuing_generation", FIXTURE)):
            metrics, reused = LeagueMetrics(True), 0
            for _ in range(samples):
                cancellation, detected = Event(), []
                process = session.process
                def ready(chunk):
                    chunk.close()
                    if not detected:
                        detected.append(time.perf_counter_ns())
                        cancellation.set()
                try:
                    tts.stream_speech(phrase, ready, cancellation)
                except OperationCancelled:
                    pass
                if not detected:
                    raise RuntimeError("Native synthesis did not provide a chunk")
                metrics.record("cancel_to_generator_released", time.perf_counter_ns() - detected[0])
                tts.warm_up()
                metrics.record("cancel_to_next_voice_ready", time.perf_counter_ns() - detected[0])
                reused += session.process is process
            result["cases"][name] = {"reused_workers": reused, "metrics": metrics.report()}
        if audio:
            from core.latency import LatencyTrace, trace_scope
            collector = LeagueMetrics(True)
            for _ in range(samples):
                cancellation = Event()
                trace = LatencyTrace(collector)
                timer = None
                def interrupt():
                    trace.mark("interruption_detected")
                    cancellation.set()
                def started():
                    nonlocal timer
                    timer = Timer(.1, interrupt)
                    timer.start()
                try:
                    with trace_scope(trace):
                        with tts.prepare_speech("This is a fixed audio interruption test. The next words should stop.",
                                                cancellation, on_playback_start=started) as prepared:
                            prepared.play()
                except OperationCancelled:
                    pass
                finally:
                    if timer is not None:
                        timer.cancel()
                        timer.join()
            result["device_abort"] = {"kind": "real PortAudio abort-return proxy, not acoustic sound cessation",
                                      "metrics": collector.report()}
        return result
    finally:
        config.TTS_CANCEL_DRAIN_SECONDS = previous
        session.shutdown()


def vad_sweep(recording):
    """Sample-based endpoint experiment, not paced or acoustic latency."""
    import numpy as np
    import torch
    from silero_vad import VADIterator
    from voice import voice
    voice.load_models()
    pcm = read_recording(recording)
    non_silent = np.flatnonzero(np.abs(pcm) > 32 / 32768)
    if not len(non_silent):
        raise ValueError("No non-silent samples in the fixture")
    last_sound = int(non_silent[-1])
    pcm = np.concatenate((pcm, np.zeros(32000, dtype=np.float32)))
    result = {"kind": "real Silero on supplied recording, accelerated replay; PCM threshold is an estimate",
              "cases": {}}
    for silence in (250, 350, 500, 650):
        detector = VADIterator(voice.vad_iterator.model, threshold=.5, sampling_rate=16000,
                               min_silence_duration_ms=silence, speech_pad_ms=200)
        ends, detected = [], []
        for end in range(512, len(pcm) + 1, 512):
            event = detector(torch.from_numpy(pcm[end-512:end]), return_seconds=False)
            if event and "end" in event:
                ends.append(event["end"] - detector.speech_pad_samples)
                detected.append(end)
        result["cases"][str(silence)] = {"utterances": len(ends),
            "endpoints_before_last_sound": sum(end < last_sound - 3200 for end in ends),
            "final_endpoint_after_last_sound_ms": ((detected[-1] - last_sound) / 16 if detected else None)}
    voice.vad_iterator.reset_states()
    return result


def stt_corpus_bench(samples):
    """A fixed synthetic accuracy check; never advertised as human/noise coverage."""
    from voice import voice, tts
    from voice.kokoro_session import session
    phrases = ("Explain gravity in one short sentence.", "What role did I get?", "Who did I lock in?",
               "Which champions were banned?", "What was the last objective?", "Why does the sky look blue?",
               "How can I organize my desk?", "Tell me how photosynthesis works.", "What is a Python dictionary?",
               "How does a computer store numbers?", "Compare compound interest and simple interest.",
               "What is the difference between mass and weight?", "Explain the water cycle.",
               "How do I improve at chess?", "Why do we have seasons?", "Tell me about the solar system.",
               "What makes a good team?", "How do I plan a busy day?", "What is a light year?",
               "Explain why the moon has phases.")
    previous = config.WHISPER_WITHOUT_TIMESTAMPS
    metrics = {name: LeagueMetrics(True) for name in ("timestamps", "text_only")}
    errors, words, disagreements = {name: 0 for name in metrics}, 0, 0
    def tokens(text):
        return re.findall(r"\w+", text.casefold())
    def distance(a, b):
        row = list(range(len(b) + 1))
        for i, word in enumerate(a, 1):
            next_row = [i]
            for j, other in enumerate(b, 1):
                next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j-1] + (word != other)))
            row = next_row
        return row[-1]
    try:
        voice.load_models()
        tts.warm_up()
        with tempfile.TemporaryDirectory(prefix="capri-stt-corpus-") as directory:
            path = Path(directory) / "fixture.wav"
            for index in range(samples):
                phrase = phrases[index % len(phrases)]
                tts._synthesize(phrase, path, Event())
                pcm = read_recording(path)
                results = {}
                for name in metrics:
                    config.WHISPER_WITHOUT_TIMESTAMPS = name == "text_only"
                    text = timed(metrics[name], "stt_wall", lambda: voice.transcribe_audio(pcm, Event()))
                    results[name] = tokens(text)
                    errors[name] += distance(tokens(phrase), results[name])
                words += len(tokens(phrase))
                disagreements += results["timestamps"] != results["text_only"]
        return {"kind": "real Whisper on fixed varied Kokoro questions; no human/noise/accent claim",
                "samples_per_variant": samples, "reference_words": words,
                "word_edit_errors": errors, "variant_transcript_disagreements": disagreements,
                "metrics": {name: value.report() for name, value in metrics.items()}}
    finally:
        config.WHISPER_WITHOUT_TIMESTAMPS = previous
        session.shutdown()


def replay_bench(samples, model, question="Explain gravity in one short sentence.", recording=None,
                 consecutive=False, league_fixture=False, companion=False, compare_stt=False, desktop_ui=False,
                   audio=False, compare_threads=None, loopback=False, loopback_endpoint=None, compare_output=False,
                   warmup_turns=0):
    """Real engines on explicit replay; optional output/loopback, no microphone."""
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
    replay_metrics = metrics
    from core.latency import VoiceMetrics
    variants = ([f"threads{count}" for count in compare_threads] if compare_threads else
                ["original_driver", "shared_wasapi"] if compare_output else
                ["timestamps", "text_only"] if compare_stt else [])
    cases = {name: {"timings": LeagueMetrics(True), "stages": VoiceMetrics(True)} for name in variants}
    thread_models = {}
    original_recognizer = voice.whisper_model
    probe = None
    old_model = config.CONVERSATION_MODEL
    old_stt = getattr(config, "WHISPER_WITHOUT_TIMESTAMPS", False)
    old_wasapi = config.TTS_WASAPI_ENABLED
    old_database = database.DATABASE
    old_play, old_chat = tts._play_wav, conversation.request_chat
    window = app = None
    ui_settings = None
    config.CONVERSATION_MODEL = model
    def seeded_chat(**kwargs):
        kwargs["options"] = {**kwargs.get("options", {}), "seed": 42}
        return old_chat(**kwargs)
    conversation.request_chat = seeded_chat
    try:
        voice.load_models()
        original_recognizer = voice.whisper_model
        if compare_threads:
            from faster_whisper import WhisperModel
            for count in compare_threads:
                thread_models[f"threads{count}"] = (original_recognizer
                    if config.WHISPER_DEVICE == "cpu" and config.WHISPER_CPU_THREADS == count else
                    WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=count, local_files_only=True))
        tts.warm_up()
        if not league_fixture:
            old_chat(model=conversation.conversation_model(), messages=[], keep_alive=config.MODEL_KEEP_ALIVE)
        with tempfile.TemporaryDirectory(prefix="capri-voice-fixture-") as directory:
            database.DATABASE = Path(directory) / "memory.db"  # Never read personal facts in a benchmark.
            path = Path(directory) / "fixture.wav"
            if recording is None:
                tts._synthesize(question, path, Event())
            pcm = read_recording(recording or path)
            signal = np.concatenate((np.zeros(3200, dtype=np.float32), pcm, np.zeros(16000, dtype=np.float32)))
            last_sound = int(np.flatnonzero(np.abs(signal) > 32 / 32768)[-1])
            # Warm recognition independently; no recorded benchmark sample here.
            voice.transcribe_audio(signal, Event())
            for recognizer in thread_models.values():
                voice.whisper_model = recognizer
                voice.transcribe_audio(signal, Event())
            if loopback:
                from benchmarks.wasapi_loopback import LoopbackProbe
                probe = LoopbackProbe(loopback_endpoint)
                probe.__enter__()
            history = [{"role": "system", "content": config.SYSTEM_PROMPT}]
            if companion or desktop_ui:
                import os
                os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
                from PySide6.QtWidgets import QApplication
                from PySide6.QtGui import QFontDatabase
                import gui.window as gui
                app = QApplication.instance()
                if app is None:
                    app = QApplication([])
                    for filename in ("segoeui.ttf", "segoeuib.ttf"):
                        font = Path("C:/Windows/Fonts") / filename
                        if font.is_file():
                            QFontDatabase.addApplicationFont(str(font))
                app.setQuitOnLastWindowClosed(False)
                ui_settings = {key: getattr(config, key) for key in ("VOICE_PRELOAD_ON_STARTUP", "LEAGUE_EVENTS_ENABLED",
                               "LEAGUE_LIVE_DATA_ENABLED", "LEAGUE_CHAMP_SELECT_ENABLED")}
                vision = gui.VISION_ENABLED_BY_DEFAULT
                gui.VISION_ENABLED_BY_DEFAULT = False
                try:
                    for key in ui_settings:
                        setattr(config, key, False)
                    window = gui.CapriWindow(history, Context())
                    window.show()
                    app.processEvents()
                    window.set_league_companion_mode(companion)
                    app.processEvents()
                finally:
                    gui.VISION_ENABLED_BY_DEFAULT = vision
                    for key, value in ui_settings.items():
                        setattr(config, key, value)
            references, mismatches = {}, 0
            paired_dac = {name: [] for name in variants}
            paired_loopback = {name: [] for name in variants}
            warmups = max(int(warmup_turns), int(bool(compare_threads or compare_output)))
            for trial in range((samples + warmups) * (len(variants) or 1)):
                warming = trial < warmups * (len(variants) or 1)
                if warming:
                    metrics = LeagueMetrics(True)
                elif not variants:
                    metrics = replay_metrics
                if variants:
                    # AB then BA reduces systematic order effects across pairs.
                    position = trial % len(variants)
                    order = variants if (trial // len(variants)) % 2 == 0 else list(reversed(variants))
                    name = order[position]
                    if compare_threads:
                        voice.whisper_model = thread_models[name]
                    elif compare_stt:
                        config.WHISPER_WITHOUT_TIMESTAMPS = name == "text_only"
                    if compare_output:
                        config.TTS_WASAPI_ENABLED = name == variants[1]
                    metrics = LeagueMetrics(True) if warming else cases[name]["timings"]
                trace = None
                if getattr(config, "VOICE_LATENCY_METRICS_ENABLED", False):
                    from core.latency import LatencyTrace, start_trace, use_trace
                    trace = (LatencyTrace(VoiceMetrics(True)) if warming else
                             LatencyTrace(cases[name]["stages"]) if variants else start_trace())
                    use_trace(trace)
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
                    trace.mark("capture_stopped")  # Replay only: no input-device teardown.
                    trace.mark("capture_complete")
                metrics.record("fixture_speech_end_to_vad", time.perf_counter_ns() - speech_end)
                transcript = timed(metrics, "fixture_stt", lambda: voice.transcribe_audio(signal[:end], Event()))
                if variants and not warming:
                    pair = trial // len(variants)
                    if pair in references:
                        mismatches += transcript != references[pair]
                    else:
                        references[pair] = transcript
                first = []
                def ready(path, cancellation, on_playback_start=None):
                    check = time.perf_counter_ns()
                    initial = not first
                    if initial:
                        first.append(True)
                        metrics.record("fixture_speech_end_to_first_audio_ready", check - speech_end)
                        if probe:
                            probe.arm()
                    if audio:
                        old_play(path, cancellation, on_playback_start)
                        if initial and probe:
                            onset = probe.result()
                            if onset is None:
                                raise RuntimeError("No loopback output onset; check selected render endpoint/mute")
                            if trace is not None:
                                trace.mark("loopback_onset", onset[0])
                                trace.mark("loopback_packet_received", onset[1])
                    # Without --audio, don't fake a playback-start callback.
                tts._play_wav = ready
                context = window.context if window is not None else Context()
                if window is not None and not consecutive:
                    context.interaction = InteractionState()  # Independent fixture turns, not production repeat suppression.
                if league_fixture:
                    # Known read-only API fixture, never a claim of a live match.
                    context.league.champ_select.update(phase="ChampSelect", session={"localPlayerCellId": 1,
                        "myTeam": [{"cellId": 1, "assignedPosition": "UTILITY", "championId": 350}],
                        "actions": []})
                    context.check_screen = lambda *args: {"status": "checked"}
                messages = history if consecutive else [{"role": "system", "content": config.SYSTEM_PROMPT}]
                text = ("Say that again: " + transcript) if consecutive and len(history) > 1 else transcript
                class ReplayWorker(ChatWorker):
                    def run(self):
                        from core.latency import use_trace
                        use_trace(trace)
                        super().run()
                worker = ReplayWorker(messages, context, text, True)
                errors = []
                worker.error.connect(lambda error: errors.append(type(error).__name__))
                if window is not None:
                    window.messages = messages
                    window.chat_worker = worker
                    window._connect_worker(worker)
                    worker.start()
                    while window.chat_worker is not None:
                        app.processEvents()
                        time.sleep(.001)
                    app.processEvents()
                else:
                    worker.run()
                if errors or not first:
                    raise RuntimeError("Replayed fixture did not reach audio readiness")
                if variants and not warming and trace is not None:
                    for mark_name, target in (("device_playback", paired_dac), ("loopback_onset", paired_loopback)):
                        if mark_name in trace._marks:
                            target[name].append((trace._marks[mark_name] - speech_end) / 1e6)
            paired = {}
            if len(variants) == 2:
                from benchmark_playback import paired_interval
                for label, readings in (("dac", paired_dac), ("loopback", paired_loopback)):
                    if all(len(readings[name]) == samples for name in variants):
                        paired[label] = paired_interval(*(readings[name] for name in variants))
        engines = "real VAD/STT/Kokoro with mock League API" if league_fixture else "real VAD/STT/Ollama/Kokoro"
        return {"kind": engines + " on paced speech; no microphone; " +
                        ("real speaker output, DAC estimates" if audio else "no speaker output"),
                "model": "none (deterministic API fixture)" if league_fixture else conversation.conversation_model(),
                "metrics": metrics.report(),
                "consecutive_history": consecutive, "league_data": "fixture" if league_fixture else "none",
                "companion_ui": companion,
                "desktop_ui": desktop_ui,
                "comparison": {name: {"metrics": data["timings"].report(), "stages": data["stages"].report()}
                               for name, data in cases.items()},
                "paired_transcript_mismatches": mismatches if variants else None,
                "paired_reduction": paired,
                "loopback_format": probe.format if probe else None,
                "comparison_settings": {"cpu_threads": compare_threads, "original_driver_vs_wasapi": compare_output},
                "limits": "Fixture speech end uses a PCM amplitude threshold, not a human/acoustic measurement."}
    finally:
        try:
            if probe:
                probe.__exit__()
        finally:
            if compare_threads:
                voice.whisper_model = original_recognizer
            if window is not None:
                window.close()
                # Normal close cooperatively cancels any failed/in-flight fixture.
                while window.chat_worker is not None:
                    app.processEvents()
                    time.sleep(.005)
                app.processEvents()
            config.CONVERSATION_MODEL = old_model
            config.WHISPER_WITHOUT_TIMESTAMPS = old_stt
            config.TTS_WASAPI_ENABLED = old_wasapi
            database.DATABASE = old_database
            tts._play_wav, conversation.request_chat = old_play, old_chat
            session.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--tts", action="store_true")
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--ollama", action="store_true")
    parser.add_argument("--first-text-only", action="store_true", help="Cancel Ollama after first useful text; TTFT only, no decode-throughput claim")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--contention", action="store_true")
    parser.add_argument("--followups", action="store_true", help="Real-model comparison of 2s/6s quiet policies using a paced 3s follow-up gap")
    parser.add_argument("--replay", action="store_true", help="Paced synthesized speech through real VAD/STT/model/TTS; no devices")
    parser.add_argument("--profile", action="store_true", help="Include production numeric stage and native CPU metrics")
    parser.add_argument("--question", help="Fixed benchmark question; keep identical for before/after")
    parser.add_argument("--recording", type=Path, help="Optional supplied 16-bit PCM WAV instead of generated speech")
    parser.add_argument("--consecutive", action="store_true", help="Replay retains conversation history; explicitly requests repetition")
    parser.add_argument("--league-fixture", action="store_true", help="Replay deterministic role question from mock API data")
    parser.add_argument("--companion", action="store_true", help="Replay with real offscreen League Companion UI")
    parser.add_argument("--desktop-ui", action="store_true", help="Replay with the same offscreen GUI in normal desktop mode")
    parser.add_argument("--compare-stt", action="store_true", help="Interleave timestamp/text-only recognition with identical paced speech; samples per variant")
    parser.add_argument("--compare-stt-threads", type=int, nargs=2, metavar=("BEFORE", "AFTER"), help="Paired CPU Whisper thread comparison; benchmark-only model instances")
    parser.add_argument("--compare-output-backend", action="store_true", help="Paired original driver / preferred shared WASAPI comparison")
    parser.add_argument("--loopback", action="store_true", help="With --replay --audio, passively measure Windows system-mix output onset")
    parser.add_argument("--loopback-endpoint", help="Optional Windows render endpoint ID")
    parser.add_argument("--warmup-turns", type=int, default=0, help="Unmeasured full replay turns before the requested samples")
    parser.add_argument("--interrupt", action="store_true", help="Real TTS cancellation/reload probe, no devices")
    parser.add_argument("--cancel-drain-ms", type=float, choices=(0, 50), help="Compare cancellation acknowledgment drain")
    parser.add_argument("--vad-sweep", action="store_true", help="Compare 250/350/500/650 ms endpointing on --recording")
    parser.add_argument("--stt-corpus", action="store_true", help="Compare both Whisper modes on varied fixed generated questions")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("--samples must be 1..100")
    if args.vad_sweep and args.recording is None:
        parser.error("--vad-sweep requires --recording with representative pauses")
    if args.compare_stt and args.consecutive:
        parser.error("--compare-stt uses independent identical turns; run --consecutive separately")
    if args.compare_stt_threads and (args.compare_stt or args.consecutive or any(n not in (1,2,4,6,8) for n in args.compare_stt_threads)):
        parser.error("Use two CPU thread counts (1,2,4,6,8) without --compare-stt/--consecutive")
    if args.loopback and not (args.replay and args.audio):
        parser.error("--loopback requires explicit --replay --audio")
    if args.compare_output_backend and (args.compare_stt or args.consecutive):
        parser.error("--compare-output-backend uses independent paired turns, without --compare-stt/--consecutive")
    if args.compare_stt_threads and len(set(args.compare_stt_threads)) != 2:
        parser.error("Use different thread counts, or --compare-output-backend alone")
    if args.loopback:
        args.profile = True
    if not 0 <= args.warmup_turns <= 10:
        parser.error("--warmup-turns must be 0..10")
    if args.profile:
        config.VOICE_LATENCY_METRICS_ENABLED = True
    result = {"python": platform.python_version(), "hardware": hardware(), "synthetic": synthetic(args.samples),
              "settings": {"conversation_model": config.CONVERSATION_MODEL, "primary_model": config.MODEL,
                           "voice_speaker": config.KOKORO_SPEAKER_ID, "voice_speed": config.KOKORO_SPEED,
                           "tts_threads": getattr(config, "KOKORO_NUM_THREADS", 2),
                           "whisper_cpu_threads": config.WHISPER_CPU_THREADS,
                           "wasapi_preferred": config.TTS_WASAPI_ENABLED,
                           "whisper_device": getattr(config, "WHISPER_DEVICE", "auto"),
                           "native_tts_chunks": getattr(config, "TTS_NATIVE_STREAMING_ENABLED", False),
                           "silence_ms": config.VOICE_SILENCE_MS},
              "limits": ["No human speech-end/acoustic latency without a microphone/loopback measurement.",
                         "Synthetic token timing is not measured Ollama throughput."]}
    if args.tts or (args.audio and not args.interrupt and not args.replay):
        result["tts"] = tts_bench(args.samples, args.audio)
    if args.ollama:
        result["ollama"] = ollama_bench(args.samples, args.model, args.question or QUESTION, args.first_text_only)
    if args.contention or args.followups:
        result["contention"] = contention_bench(args.model, args.samples, args.followups)
    if args.replay:
        result["replay"] = replay_bench(args.samples, args.model,
            args.question or ("What role did I get?" if args.league_fixture else "Explain gravity in one short sentence."),
            args.recording, args.consecutive, args.league_fixture, args.companion, args.compare_stt, args.desktop_ui,
            args.audio, args.compare_stt_threads, args.loopback, args.loopback_endpoint, args.compare_output_backend,
            args.warmup_turns)
    if args.interrupt:
        result["interruption"] = interruption_bench(args.samples, args.cancel_drain_ms, args.audio)
    if args.vad_sweep:
        result["vad"] = vad_sweep(args.recording)
    if args.stt_corpus:
        result["stt_corpus"] = stt_corpus_bench(args.samples)
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

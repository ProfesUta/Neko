"""Keep the official Capri Kokoro voice warm and terminate synthesis on Stop."""

import atexit
import json
import logging
import math
import os
import subprocess
import sys
import time
import config
from collections import deque
from contextlib import suppress
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

from core.cancellation import OperationCancelled, check_cancelled
from core.latency import current_trace
from voice.kokoro_worker import ASSET_FILES, CHAMPION_LEXICON, CONVERSATION_LEXICON, validate_assets, validate_request, validate_settings


def _record_native_timing(trace, message):
    if trace is None or not isinstance(timing := message.get("timing"), dict):
        return
    fields = {"generate_enter_ns": "native_generate_enter", "callback_ns": "native_first_callback",
              "generate_return_ns": "native_generate_return", "pcm_ns": "native_pcm_ready", "wav_ns": "native_wav_ready"}
    for field, name in fields.items():
        if type(timing.get(field)) is int:
            trace.mark(name, timing[field])
    if type(message.get("received_ns")) is int:
        trace.mark("native_chunk_received", message["received_ns"])
    trace.mark("native_chunk_handled")


class KokoroSession:
    """Serialize synthesis and own the resident worker's complete lifetime."""

    name = "Kokoro"
    worker_filename = "kokoro_worker.py"

    def __init__(self):
        self._lock = Lock()
        self.process = None
        self._signature = None
        self._messages = None
        self._reader = None
        self._read_stop = Event()
        self._diagnostics = deque(maxlen=8)
        self._options_warning = None

    def _shutdown_locked(self):
        process, self.process = self.process, None
        self._signature = None
        self._read_stop.set()
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if self._reader is not None:
            self._reader.join(timeout=.2)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        self._reader = None

    def shutdown(self):
        with self._lock:
            self._shutdown_locked()

    def _read_messages(self, process, messages, diagnostics, stopped):
        def send(value):
            while not stopped.is_set():
                try:
                    messages.put(value, timeout=.025)
                    return
                except Full:
                    continue
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    diagnostics.append(line.strip()[:500])
                    continue
                if isinstance(value, dict):
                    if isinstance(value.get("timing"), dict):
                        value["received_ns"] = time.perf_counter_ns()
                    send(value)
                if stopped.is_set():
                    break
        finally:
            send({"error": f"{self.name} worker exited. " + " ".join(diagnostics)})

    def _wait(self, cancellation, deadline, on_chunk=None):
        while True:
            check_cancelled(cancellation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Speech generation took too long. Please try again.")
            try:
                message = self._messages.get(timeout=min(0.05, remaining))
            except Empty:
                continue
            check_cancelled(cancellation)
            if "error" in message:
                raise RuntimeError(f"{self.name} failed: {message['error']}")
            if "chunk" in message:
                if on_chunk is None:
                    raise RuntimeError("Unexpected streamed audio from Kokoro")
                on_chunk(message)
                continue
            return message

    def _resources(self, asset_dir):
        directory = validate_assets(asset_dir)
        assets = [directory / name for name in ASSET_FILES] + [directory / "espeak-ng-data", CHAMPION_LEXICON, CONVERSATION_LEXICON]
        arguments = [str(directory), str(config.KOKORO_NUM_THREADS)]
        if config.KOKORO_CPU_OPTIONS and str(config.KOKORO_CPU_OPTIONS).casefold() != "none":
            options = Path(config.KOKORO_CPU_OPTIONS).expanduser().resolve()
            if options.is_file():
                assets.append(options)
                arguments.append(str(options))
                self._options_warning = None
            elif self._options_warning != options:
                logging.warning("Kokoro CPU options unavailable; retaining plain CPU")
                self._options_warning = options
        personal = config.PERSONAL_PRONUNCIATIONS_FILE
        if personal and str(personal).casefold() != "none":
            path = Path(personal).expanduser().resolve()
            if path.is_file():
                assets.append(path)
                arguments.extend(("--personal-pronunciations", str(path)))
        return assets, arguments

    def _prepare_locked(self, asset_dir, cancellation, deadline):
        assets, arguments = self._resources(asset_dir)
        signature = (tuple(arguments), *((path.stat().st_mtime_ns, path.stat().st_size) for path in assets))
        if self.process is not None and self.process.poll() is None and signature == self._signature:
            return
        self._shutdown_locked()
        self._messages = Queue(maxsize=8)
        self._read_stop = Event()
        self._diagnostics = deque(maxlen=8)
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).parent / self.worker_filename), *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._reader = Thread(target=self._read_messages,
                              args=(self.process, self._messages, self._diagnostics, self._read_stop),
                              name=f"Capri {self.name} output", daemon=True)
        self._reader.start()
        ready = self._wait(cancellation, deadline)
        if ready.get("ready") is not True:
            raise RuntimeError(f"{self.name} did not finish loading its voice")
        self._signature = signature
        logging.info("%s speech worker ready", self.name)

    def request(self, asset_dir, cancellation=None, timeout=90, *, text=None, output=None, speaker=53, speed=1.1,
                on_chunk=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 3600 or not math.isfinite(timeout)):
            raise ValueError("Speech timeout must be a finite number between 0 and 3600 seconds")
        speaker, speed = validate_settings(speaker, speed)
        request = None
        trace = current_trace()
        if text is not None:
            text, path, speaker, speed = validate_request(text, output, speaker, speed)
            request = {"text": text, "output": str(path), "speaker": speaker, "speed": speed}
            if on_chunk is not None:
                request["stream"] = True
            if trace is not None:
                request["profile"] = True
        deadline = time.monotonic() + timeout
        while not self._lock.acquire(timeout=0.05):
            check_cancelled(cancellation)
            if time.monotonic() >= deadline:
                raise RuntimeError("Speech output is busy. Please try again.")
        in_flight = False
        generation_complete = False
        def chunk_ready(message):
            nonlocal generation_complete
            generation_complete = message.get("generation_complete") is True
            _record_native_timing(trace, message)
            on_chunk(message)
        try:
            check_cancelled(cancellation)
            self._prepare_locked(asset_dir, cancellation, deadline)
            if request is not None:
                check_cancelled(cancellation)
                if trace is not None:
                    trace.mark("tts_worker_dispatched")
                in_flight = True
                self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
                self.process.stdin.flush()
                result = self._wait(cancellation, deadline, chunk_ready if on_chunk is not None else None)
                in_flight = False
                if result.get("ok") is not True:
                    raise RuntimeError(f"{self.name} did not confirm speech generation")
                if trace is not None:
                    _record_native_timing(trace, result)
                    for key in ("synthesis_wall_ns", "synthesis_cpu_ns"):
                        if isinstance(result.get(key), int):
                            trace.collector.record("native_tts_" + key.removesuffix("_ns"), result[key])
                    if type(result.get("first_callback_cpu_ns")) is int:
                        trace.collector.record("native_tts_first_callback_cpu", result["first_callback_cpu_ns"])
        except OperationCancelled:
            # Audio has its own cancellation token and stops immediately. A
            # native chunk can race the final 'ok' by a few milliseconds. Drain
            # that acknowledgment before throwing away a fully loaded model.
            if in_flight:
                try:
                    grace = max(0, min(.1, config.TTS_CANCEL_DRAIN_SECONDS)) if generation_complete else 0
                    result = self._wait(None, min(deadline, time.monotonic() + grace), lambda message: None)
                    if result.get("ok") is not True:
                        self._shutdown_locked()
                except Exception:
                    self._shutdown_locked()
            elif self._signature is None:
                self._shutdown_locked()  # A cancelled model load is incomplete.
            raise
        except BaseException:
            self._shutdown_locked()
            raise
        finally:
            self._lock.release()


session = KokoroSession()
atexit.register(session.shutdown)

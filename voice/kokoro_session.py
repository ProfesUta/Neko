"""Keep the official Capri Kokoro voice warm and terminate synthesis on Stop."""

import atexit
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections import deque
from contextlib import suppress
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread

from core.cancellation import check_cancelled
from voice.kokoro_worker import ASSET_FILES, CHAMPION_LEXICON, validate_assets, validate_request, validate_settings


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
        self._diagnostics = deque(maxlen=8)

    def _shutdown_locked(self):
        process, self.process = self.process, None
        self._signature = None
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

    def _read_messages(self, process, messages, diagnostics):
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    diagnostics.append(line.strip()[:500])
                    continue
                if isinstance(value, dict):
                    messages.put(value)
        finally:
            messages.put({"error": f"{self.name} worker exited. " + " ".join(diagnostics)})

    def _wait(self, cancellation, deadline):
        while True:
            check_cancelled(cancellation)
            if time.monotonic() >= deadline:
                raise RuntimeError("Speech generation took too long. Please try again.")
            try:
                message = self._messages.get(timeout=0.05)
            except Empty:
                continue
            check_cancelled(cancellation)
            if "error" in message:
                raise RuntimeError(f"{self.name} failed: {message['error']}")
            return message

    def _resources(self, asset_dir):
        directory = validate_assets(asset_dir)
        assets = [directory / name for name in ASSET_FILES] + [directory / "espeak-ng-data", CHAMPION_LEXICON]
        return assets, [str(directory)]

    def _prepare_locked(self, asset_dir, cancellation, deadline):
        assets, arguments = self._resources(asset_dir)
        signature = (tuple(arguments), *((path.stat().st_mtime_ns, path.stat().st_size) for path in assets))
        if self.process is not None and self.process.poll() is None and signature == self._signature:
            return
        self._shutdown_locked()
        self._messages = Queue()
        self._diagnostics = deque(maxlen=8)
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).with_name(self.worker_filename)), *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._reader = Thread(target=self._read_messages,
                              args=(self.process, self._messages, self._diagnostics),
                              name=f"Capri {self.name} output", daemon=True)
        self._reader.start()
        ready = self._wait(cancellation, deadline)
        if ready.get("ready") is not True:
            raise RuntimeError(f"{self.name} did not finish loading its voice")
        self._signature = signature
        logging.info("%s speech worker ready", self.name)

    def request(self, asset_dir, cancellation=None, timeout=90, *, text=None, output=None, speaker=53, speed=1.1):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 3600 or not math.isfinite(timeout)):
            raise ValueError("Speech timeout must be a finite number between 0 and 3600 seconds")
        speaker, speed = validate_settings(speaker, speed)
        request = None
        if text is not None:
            text, path, speaker, speed = validate_request(text, output, speaker, speed)
            request = {"text": text, "output": str(path), "speaker": speaker, "speed": speed}
        deadline = time.monotonic() + timeout
        while not self._lock.acquire(timeout=0.05):
            check_cancelled(cancellation)
            if time.monotonic() >= deadline:
                raise RuntimeError("Speech output is busy. Please try again.")
        try:
            check_cancelled(cancellation)
            self._prepare_locked(asset_dir, cancellation, deadline)
            if request is not None:
                self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
                self.process.stdin.flush()
                if self._wait(cancellation, deadline).get("ok") is not True:
                    raise RuntimeError(f"{self.name} did not confirm speech generation")
        except BaseException:
            self._shutdown_locked()
            raise
        finally:
            self._lock.release()


session = KokoroSession()
atexit.register(session.shutdown)

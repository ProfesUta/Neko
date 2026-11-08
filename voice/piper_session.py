"""One reusable Piper subprocess, still immediately terminable on Stop."""

import atexit
import json
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


class PiperSession:
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
            self._reader.join(timeout=0.2)
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
            messages.put({"error": "Piper worker exited. " + " ".join(diagnostics)})

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
                raise RuntimeError(f"Piper failed: {message['error']}")
            return message

    def _prepare_locked(self, voice_path, cancellation, deadline):
        path = Path(voice_path).resolve()
        settings = Path(str(path) + ".json")
        signature = (str(path), path.stat().st_mtime_ns, settings.stat().st_mtime_ns)
        if self.process is not None and self.process.poll() is None and signature == self._signature:
            return
        self._shutdown_locked()
        self._messages = Queue()
        self._diagnostics = deque(maxlen=8)
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).with_name("piper_worker.py")), str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._reader = Thread(target=self._read_messages,
                              args=(self.process, self._messages, self._diagnostics),
                              name="Capri speech output", daemon=True)
        self._reader.start()
        if self._wait(cancellation, deadline).get("ready") is not True:
            raise RuntimeError("Piper did not finish loading its voice")
        self._signature = signature

    def request(self, voice_path, cancellation=None, timeout=90, *, text=None, output=None):
        deadline = time.monotonic() + timeout
        while not self._lock.acquire(timeout=0.05):
            check_cancelled(cancellation)
            if time.monotonic() >= deadline:
                raise RuntimeError("Speech output is busy. Please try again.")
        try:
            check_cancelled(cancellation)
            self._prepare_locked(voice_path, cancellation, deadline)
            if text is not None:
                self.process.stdin.write(json.dumps({"text": text, "output": str(Path(output).resolve())}) + "\n")
                self.process.stdin.flush()
                if self._wait(cancellation, deadline).get("ok") is not True:
                    raise RuntimeError("Piper did not confirm speech generation")
        except BaseException:
            self._shutdown_locked()
            raise
        finally:
            self._lock.release()


session = PiperSession()
atexit.register(session.shutdown)

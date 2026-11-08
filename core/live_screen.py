"""Continuous capture with bounded, recent-only frame history; no inference queue."""

import time
from collections import deque
from dataclasses import dataclass
from threading import Event, Lock, Thread


class CaptureLayoutChanged(Exception):
    """Reopen capture handles after a display topology change."""


@dataclass(frozen=True)
class FrameBatch:
    epoch: int
    frames: tuple


class FrameHistory:
    def __init__(self, max_frames=24, max_seconds=3, max_bytes=24 * 1024 * 1024):
        self.max_frames = max(1, int(max_frames))
        self.max_seconds = max(0.1, float(max_seconds))
        self.max_bytes = max(1, int(max_bytes))
        self._lock = Lock()
        self._frames = deque()
        self._bytes = 0
        self._key = None
        self._epoch = 0

    def _clear(self):
        self._frames.clear()
        self._bytes = 0
        self._key = None
        self._epoch += 1

    def clear(self):
        with self._lock:
            self._clear()

    def put(self, frame):
        size = len(frame.jpeg) + sum(map(len, frame.images))
        key = (frame.source_id, frame.context_key)
        with self._lock:
            if size > self.max_bytes:
                self._clear()
                raise ValueError("A live frame exceeds the configured memory limit")
            if key != self._key:
                self._clear()
                self._key = key
            self._frames.append((frame, size))
            self._bytes += size
            while (len(self._frames) > self.max_frames or self._bytes > self.max_bytes
                   or self._frames[0][0].captured_at < frame.captured_at - self.max_seconds):
                _old, old_size = self._frames.popleft()
                self._bytes -= old_size

    def sample(self, count=2, spacing=1, max_age=1, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if not self._frames or not 0 <= now - self._frames[-1][0].captured_at <= max_age:
                return FrameBatch(self._epoch, ())
            selected = [self._frames[-1][0]]
            for frame, _size in reversed(list(self._frames)[:-1]):
                if len(selected) >= max(1, count):
                    break
                if (now - frame.captured_at <= self.max_seconds
                        and selected[-1].captured_at - frame.captured_at >= spacing):
                    selected.append(frame)
            return FrameBatch(self._epoch, tuple(reversed(selected)))

    def status(self):
        with self._lock:
            age = time.monotonic() - self._frames[-1][0].captured_at if self._frames else None
            times = [frame.captured_at for frame, _size in self._frames]
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0
            return {"epoch": self._epoch, "frames": len(times), "bytes": self._bytes,
                    "frame_age_seconds": round(age, 3) if age is not None else None,
                    "capture_fps": round(fps, 1)}


class LiveScreenStream:
    def __init__(self, capture_factory, history, fps=8, on_error=None, retry_seconds=1, on_recovered=None):
        self.history = history
        self.capture_factory = capture_factory
        self.period = 1 / max(1, min(float(fps), 30))
        self.on_error = on_error
        self.on_recovered = on_recovered
        self.retry_seconds = max(0.05, retry_seconds)
        self._stop = Event()
        self._thread = None
        self.error = ""

    def start(self):
        if self._thread is not None:
            raise RuntimeError("This screen stream has already been started")
        self._thread = Thread(target=self._run, name="Capri live screen capture", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.history.clear()

    def join(self):
        if self._thread is not None:
            self._thread.join()

    def status(self):
        result = self.history.status()
        result.update(running=bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),
                      error=self.error)
        result["capturing"] = bool(result["running"] and not self.error
                                   and result["frame_age_seconds"] is not None
                                   and result["frame_age_seconds"] <= max(1, self.period * 3))
        return result

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    # Handles are created, used, and closed on this one thread.
                    with self.capture_factory() as capture:
                        while not self._stop.is_set():
                            started = time.monotonic()
                            frame = capture()
                            if self._stop.is_set():
                                break
                            self.history.put(frame)
                            recovering = bool(self.error)
                            self.error = ""
                            if recovering and self.on_recovered and not self._stop.is_set():
                                self.on_recovered()
                            self._stop.wait(max(0, self.period - (time.monotonic() - started)))
                except CaptureLayoutChanged:
                    self.history.clear()
                except Exception as error:
                    self.history.clear()
                    message = f"{type(error).__name__}: {error}"
                    if message != self.error and not self._stop.is_set():
                        self.error = message
                        if self.on_error:
                            self.on_error(message)
                    self._stop.wait(self.retry_seconds)
        finally:
            self.history.clear()

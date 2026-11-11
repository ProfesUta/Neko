"""Optional headphone-mode input capture while the conversation worker replies.

One input/VAD thread, one latest-utterance slot, one existing STT consumer. No
second recognizer, background process, audio logging or automatic microphone use.
"""

import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Thread

from core.cancellation import AnyEvent, OperationCancelled, check_cancelled
from core.latency import start_trace


@dataclass(frozen=True)
class Utterance:
    audio: object
    generation: int
    started_at: float
    trace: object = None
    error: Exception | None = None


class CapturePump:
    def __init__(self, cancellation, on_start, record):
        self.stop_event = Event()
        self.cancellation = AnyEvent(cancellation, self.stop_event)
        self.on_start, self.record = on_start, record
        self.queue = Queue(maxsize=1)
        self.capturing = Event()
        self.thread = Thread(target=self._run, name="Capri microphone capture", daemon=True)

    def start(self):
        self.thread.start()

    def _publish(self, item):
        while True:
            check_cancelled(self.cancellation)
            try:
                self.queue.put_nowait(item)
                return
            except Full:
                try:
                    self.queue.get_nowait()  # A newer complete utterance supersedes obsolete input.
                except Empty:
                    pass

    def _run(self):
        try:
            while not self.cancellation.is_set():
                generation, started_at = 0, 0.0
                trace = start_trace()
                def began():
                    nonlocal generation, started_at
                    self.capturing.set()
                    started_at = time.monotonic()
                    generation = self.on_start()
                try:
                    audio = self.record(self.cancellation, on_speech_start=began)
                    if audio is not None:
                        self._publish(Utterance(audio, generation, started_at, trace))
                finally:
                    self.capturing.clear()
        except OperationCancelled:
            pass
        except Exception as error:
            try:
                self._publish(Utterance(None, 0, 0, error=error))
            except OperationCancelled:
                pass

    def take(self, cancellation, wake_event):
        while True:
            check_cancelled(cancellation)
            if wake_event.is_set():
                return None
            try:
                item = self.queue.get(timeout=.025)
            except Empty:
                continue
            if item.error is not None:
                raise item.error
            return item

    def has_input(self):
        return self.capturing.is_set() or not self.queue.empty()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError("The microphone driver did not stop capture promptly")

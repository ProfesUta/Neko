"""Small admission gate: foreground activity preempts only background inference.

No GPU monitor, scheduler thread, UI waits or model substitutions. Existing
network cancellation stops obsolete low-priority requests. Capture stays live.
"""

import time
from contextlib import contextmanager
from threading import Condition, Event
from weakref import WeakSet

import config
from core.cancellation import AnyEvent, check_cancelled
from core.latency import span


class InferencePriority:
    def __init__(self):
        self._condition = Condition()
        self._users = WeakSet()
        self._background = set()
        self._quiet_until = 0.0

    def user_started(self, owner):
        if not config.INFERENCE_PRIORITY_ENABLED:
            return
        with self._condition:
            self._users.add(owner)
            for event in self._background:
                event.set()
            self._condition.notify_all()

    def user_finished(self, owner):
        with self._condition:
            if owner in self._users:
                self._users.discard(owner)
                self._quiet_until = time.monotonic() + config.INFERENCE_BACKGROUND_QUIET_SECONDS
                self._condition.notify_all()

    def background_ready(self):
        if not config.INFERENCE_PRIORITY_ENABLED:
            return True
        with self._condition:
            return not self._users and not self._background and time.monotonic() >= self._quiet_until

    @contextmanager
    def background(self, cancellation):
        interrupted = Event()
        token = AnyEvent(cancellation, interrupted) if cancellation is not None else interrupted
        with span("background_admission_wait"):
            with self._condition:
                while config.INFERENCE_PRIORITY_ENABLED and (self._users or self._background or time.monotonic() < self._quiet_until):
                    check_cancelled(token)
                    self._condition.wait(.025)
                check_cancelled(token)
                self._background.add(interrupted)
        try:
            yield token
        finally:
            with self._condition:
                self._background.discard(interrupted)
                self._condition.notify_all()


priority = InferencePriority()

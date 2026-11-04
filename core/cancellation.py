"""Cooperative cancellation shared by model, audio, and GUI workers."""

import time


class OperationCancelled(Exception):
    """The user stopped the current operation."""


def check_cancelled(event):
    if event is not None and event.is_set():
        raise OperationCancelled()


class DeadlineEvent:
    def __init__(self, deadline):
        self.deadline = deadline

    def is_set(self):
        return time.monotonic() > self.deadline


class AnyEvent:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.02, remaining))
        return True

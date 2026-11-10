"""Opt-in bounded latency samples. Disabled path never reads the clock."""

from collections import defaultdict, deque
from threading import Lock
from time import perf_counter_ns

import config


class LeagueMetrics:
    def __init__(self, enabled=None):
        self.enabled = config.LEAGUE_LATENCY_METRICS_ENABLED if enabled is None else enabled
        self._lock = Lock()
        self._samples = defaultdict(lambda: deque(maxlen=2048))
        self._counts = defaultdict(int)
        self._maxima = defaultdict(float)

    def start(self):
        return perf_counter_ns() if self.enabled else 0

    def finish(self, name, started):
        if self.enabled and started:
            self.record(name, perf_counter_ns() - started)

    def record(self, name, nanoseconds):
        if self.enabled:
            with self._lock:
                milliseconds = max(0, nanoseconds) / 1_000_000
                self._samples[name].append(milliseconds)
                self._counts[name] += 1
                self._maxima[name] = max(self._maxima[name], milliseconds)

    def report(self):
        with self._lock:
            samples = {key: sorted(values) for key, values in self._samples.items() if values}
            counts, maxima = dict(self._counts), dict(self._maxima)
        return {key: {"count": counts[key], "retained_samples": len(values), "p50_ms": values[int((len(values)-1)*.50)],
                      "p95_ms": values[int((len(values)-1)*.95)],
                      "p99_ms": values[int((len(values)-1)*.99)], "max_ms": maxima[key]}
                for key, values in samples.items()}

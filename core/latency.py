"""Opt-in numeric telemetry. No audio, words, identities or request payloads."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from collections import deque
from time import perf_counter_ns, thread_time_ns, process_time_ns

import config
from core.league_metrics import LeagueMetrics

class VoiceMetrics(LeagueMetrics):
    def __init__(self, enabled=False):
        super().__init__(enabled)
        self._token_rates = deque(maxlen=2048)
        self._token_count = 0
        self._token_max = 0.0

    def record_tokens(self, count, nanoseconds):
        if self.enabled and count > 0 and nanoseconds > 0:
            rate = count * 1e9 / nanoseconds
            with self._lock:
                self._token_rates.append(rate)
                self._token_count += 1
                self._token_max = max(self._token_max, rate)

    def report(self):
        result = super().report()
        with self._lock:
            values = sorted(self._token_rates)
            if values:
                result["llm_decode_tokens_per_second"] = {
                    "count": self._token_count, "retained_samples": len(values),
                    **{f"p{percent}_tokens_per_second": values[int((len(values) - 1) * percent / 100)]
                       for percent in (50, 95, 99)}, "max_tokens_per_second": self._token_max}
        return result


metrics = VoiceMetrics(False)
_current = ContextVar("capri_latency_trace", default=None)

# A device DAC timestamp estimates presentation, not sound arriving at a listener.
_PAIRS = {
    "vad_detected": (("speech_end_estimate", "speech_end_estimate_to_vad"),),
    "stt_complete": (("vad_detected", "vad_to_stt_wall"),),
    "request_dispatched": (("stt_complete", "stt_to_dispatch"),),
    "llm_first_token": (("request_dispatched", "llm_ttft"),),
    "tts_submitted": (("llm_first_token", "first_token_to_tts_submit"),),
    "audio_ready": (("tts_submitted", "tts_submit_to_first_audio_ready"),
                    ("speech_end_estimate", "speech_end_estimate_to_audio_ready")),
    "device_playback": (("audio_ready", "audio_ready_to_device_playback"),
                        ("speech_end_estimate", "speech_end_estimate_to_device_playback"),
                        ("league_detected", "league_detected_to_device_playback")),
}


class LatencyTrace:
    def __init__(self, collector=metrics):
        self.collector = collector
        self._lock = Lock()
        self._marks = {}

    def mark(self, name, at=None):
        if not self.collector.enabled:
            return
        at = perf_counter_ns() if at is None else at
        with self._lock:
            if name in self._marks:
                return  # First chunk/token only, including callbacks on other threads.
            self._marks[name] = at
            for previous, metric in _PAIRS.get(name, ()):
                if previous in self._marks and at >= self._marks[previous]:
                    self.collector.record(metric, at - self._marks[previous])


def current_trace():
    return _current.get()


def start_trace():
    trace = None
    if config.VOICE_LATENCY_METRICS_ENABLED:
        metrics.enabled = True
        trace = LatencyTrace()
    _current.set(trace)
    return trace


def clear_trace():
    _current.set(None)


def use_trace(trace):
    _current.set(trace)


@contextmanager
def trace_scope(trace):
    token = _current.set(trace)
    try:
        yield
    finally:
        _current.reset(token)


def mark(name, at=None):
    trace = _current.get()
    if trace is not None:
        trace.mark(name, at)


@contextmanager
def span(name):
    trace = _current.get()
    if trace is None:
        yield
        return
    wall, cpu, process = perf_counter_ns(), thread_time_ns(), process_time_ns()
    try:
        yield
    finally:
        trace.collector.record(name + "_wall", perf_counter_ns() - wall)
        trace.collector.record(name + "_thread_cpu", thread_time_ns() - cpu)
        trace.collector.record(name + "_process_cpu", process_time_ns() - process)

"""Opt-in numeric telemetry. No audio, words, identities or request payloads."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from collections import defaultdict, deque
from time import perf_counter_ns, thread_time_ns, process_time_ns

import config
from core.league_metrics import LeagueMetrics

class VoiceMetrics(LeagueMetrics):
    def __init__(self, enabled=False):
        super().__init__(enabled)
        self._token_rates = deque(maxlen=2048)
        self._token_count = 0
        self._token_max = 0.0
        self._counters = defaultdict(int)

    def increment(self, name, count=1):
        if self.enabled:
            with self._lock:
                self._counters[name] += count

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
            if self._counters:
                result["counters"] = dict(self._counters)
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
    "visual_confirmed": (("visual_first_raw","visual_raw_to_confirmation_runtime"),),
    "visual_proposed": (("visual_confirmed","visual_confirmation_to_proposal"),),
    "visual_accepted": (("visual_proposed","visual_proposal_to_voice_acceptance"),),
    "vad_detected": (("speech_end_estimate", "speech_end_estimate_to_vad"),),
    "capture_stopped": (("vad_detected", "vad_to_capture_stopped"),),
    "capture_complete": (("capture_stopped", "capture_cleanup_and_assembly"),),
    "stt_started": (("vad_detected", "vad_to_stt_start"),
                    ("capture_complete", "capture_to_stt_start"),
                    ("interruption_detected", "interruption_to_new_stt_start")),
    "stt_complete": (("vad_detected", "vad_to_stt_wall"),),
    "request_dispatched": (("stt_complete", "stt_to_dispatch"),),
    "llm_first_token": (("request_dispatched", "llm_ttft"),),
    "tts_queued": (("llm_first_token", "first_text_to_tts_queue"),),
    "tts_submitted": (("llm_first_token", "first_token_to_tts_submit"),
                      ("visual_accepted","visual_acceptance_to_tts_dispatch"),
                      ("tts_queued", "tts_queue_wait")),
    "tts_worker_dispatched": (("tts_submitted", "tts_setup_and_lock_wait"),),
    "native_generate_enter": (("tts_worker_dispatched", "tts_dispatch_to_native_generate"),),
    "native_first_callback": (("native_generate_enter", "native_generate_to_first_callback"),),
    "native_generate_return": (("native_generate_enter", "native_generate_wall"),),
    "native_pcm_ready": (("native_first_callback", "native_callback_to_pcm"),
                         ("native_generate_return", "native_return_to_pcm"),
                         ("speech_end_estimate", "speech_end_estimate_to_native_pcm")),
    "native_wav_ready": (("native_pcm_ready", "native_pcm_to_wav"),),
    "native_chunk_received": (("native_wav_ready", "native_wav_to_parent_receive"),),
    "native_chunk_handled": (("native_chunk_received", "native_parent_queue_wait"),),
    "audio_ready": (("tts_submitted", "tts_submit_to_first_audio_ready"),
                    ("visual_proposed","visual_proposal_to_first_audio_ready"),
                    ("speech_end_estimate", "speech_end_estimate_to_audio_ready")),
    "pcm_ready": (("audio_ready", "audio_ready_to_pcm_ready"),),
    "playback_queued": (("pcm_ready", "pcm_to_playback_queue"),),
    "output_opened": (("output_open_requested", "output_stream_open"),),
    "output_started": (("output_opened", "output_stream_start"),),
    "device_first_callback": (("playback_queued", "playback_queue_to_first_callback"),),
    "device_submitted": (("pcm_ready", "pcm_ready_to_device_submission"),
                         ("playback_queued", "playback_queue_to_nonsilent_callback")),
    "device_playback": (("audio_ready", "audio_ready_to_device_playback"),
                        ("visual_proposed","visual_proposal_to_dac_estimate"),
                        ("pcm_ready", "pcm_ready_to_dac_estimate"),
                        ("device_submitted", "device_submission_to_dac_estimate"),
                        ("speech_end_estimate", "speech_end_estimate_to_device_playback"),
                        ("league_detected", "league_detected_to_device_playback")),
    "loopback_onset": (("speech_end_estimate", "speech_end_estimate_to_loopback_onset"),
                       ("pcm_ready", "pcm_ready_to_loopback_onset")),
    "loopback_packet_received": (("loopback_onset", "loopback_packet_delivery"),
                                 ("speech_end_estimate", "speech_end_estimate_to_loopback_receipt"),
                                 ("pcm_ready", "pcm_ready_to_loopback_receipt")),
    "output_abort_complete": (("interruption_detected", "interruption_to_output_abort"),),
    "interrupted_turn_stopped": (("interruption_detected", "interruption_to_turn_cleanup"),),
}


class LatencyTrace:
    def __init__(self, collector=None):
        self.collector = metrics if collector is None else collector
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

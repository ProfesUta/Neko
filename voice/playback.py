"""One device stream per reply, reused across native chunks and sentences.

The audio callback only copies PCM and publishes numeric device timing. Python
callbacks, metrics, cleanup and waiting happen on the speech worker thread.
PortAudio's DAC time estimates presentation; it is not an acoustic measurement.
"""

import time
import os
import logging
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock

import config
from core.cancellation import check_cancelled
from core.latency import current_trace, mark

_current = ContextVar("capri_output_stream", default=None)
_device_lock = Lock()
_warned_wasapi_device = None


def _output_options(sd):
    """Prefer the same endpoint's Windows shared-mode driver, never guess ties.

    MME truncates friendly names. Only a unique full/prefix match is eligible;
    ambiguous/virtual/unavailable devices retain the original default route.
    Resolve once per reply so a new selection is not pinned in a global stream.
    """
    if os.name != "nt" or not config.TTS_WASAPI_ENABLED:
        return {}
    try:
        current = sd.query_devices(kind="output")
        hosts = sd.query_hostapis()
        if hosts[current["hostapi"]]["name"] == "Windows WASAPI":
            return {"extra_settings": sd.WasapiSettings(auto_convert=True)}
        name = current["name"].casefold()
        matches = [index for index, device in enumerate(sd.query_devices())
                   if device["max_output_channels"] >= current["max_output_channels"]
                   and hosts[device["hostapi"]]["name"] == "Windows WASAPI"
                   and device["name"].casefold().startswith(name)]
        if len(matches) == 1:
            return {"device": matches[0], "extra_settings": sd.WasapiSettings(auto_convert=True)}
    except (sd.PortAudioError, ValueError, KeyError, IndexError, AttributeError):
        pass
    return {}


class _Job:
    def __init__(self, samples, cancellation):
        import numpy as np
        self.samples, self.cancellation = samples, cancellation
        self.offset = 0
        self.submitted = Event()
        self.start_ns = None
        self.submission_ns = None
        self.end_ns = None
        # Do not call silence at the start of a WAV an audible response.
        non_silent = np.flatnonzero(np.max(np.abs(samples.astype("int32")), axis=1) > 32)
        self.first_sound = int(non_silent[0]) if non_silent.size else None


class PlaybackSession:
    def __init__(self):
        self.stream = None
        self.jobs = deque()
        self.rate = self.channels = None
        self.last_end_ns = 0
        self.cancelled = False
        self._trace = None
        self._callbacks = self._underflows = self._empty_start_callbacks = 0
        self._seen_pcm = False
        self._gap_frames = 0
        self._gaps = deque(maxlen=32)
        self._first_callback_ns = None

    def _callback(self, output, frames, clock, status):
        output.fill(0)
        if self._trace is not None:
            self._callbacks += 1
            self._underflows += bool(status and getattr(status, "output_underflow", False))
            self._empty_start_callbacks += not self.jobs and not self._seen_pcm
        position = 0
        now = time.perf_counter_ns()
        if self._first_callback_ns is None:
            self._first_callback_ns = now
        dac_ns = now + int(max(0, clock.outputBufferDacTime - clock.currentTime) * 1e9)
        while self.jobs and position < frames:
            job = self.jobs[0]
            if job.cancellation is not None and job.cancellation.is_set():
                self.cancelled = True
                self.jobs.popleft()
                job.submitted.set()
                continue
            count = min(frames - position, len(job.samples) - job.offset)
            if self._trace is not None and count:
                if not job.offset and self._gap_frames:
                    self._gaps.append(self._gap_frames)
                    self._gap_frames = 0
                self._seen_pcm = True
            output[position:position + count] = job.samples[job.offset:job.offset + count]
            if (job.start_ns is None and job.first_sound is not None
                    and job.offset <= job.first_sound < job.offset + count):
                job.submission_ns = now
                job.start_ns = dac_ns + int((position + job.first_sound - job.offset) * 1e9 / self.rate)
            job.offset += count
            position += count
            if job.offset == len(job.samples):
                job.end_ns = dac_ns + int(position * 1e9 / self.rate)
                self.last_end_ns = job.end_ns
                self.jobs.popleft()
                job.submitted.set()
        if self._trace is not None and self._seen_pcm:
            self._gap_frames += frames - position

    def play(self, samples, rate, cancellation, on_started):
        import sounddevice as sd
        check_cancelled(cancellation)
        trace = current_trace()
        self._trace = trace
        if self.stream is not None and (rate, samples.shape[1]) != (self.rate, self.channels):
            raise RuntimeError("Speech format changed during a reply")
        # The first PortAudio callbacks can execute inside start(). Have PCM
        # queued already, instead of submitting an avoidable silent buffer.
        job = _Job(samples, cancellation)
        self.jobs.append(job)
        if trace is not None:
            trace.mark("playback_queued")
        if self.stream is None:
            global _warned_wasapi_device
            self.rate, self.channels = rate, samples.shape[1]
            if trace is not None:
                trace.mark("output_open_requested")
            options = _output_options(sd)
            arguments = dict(samplerate=rate, channels=self.channels, dtype="int16",
                blocksize=max(64, int(rate * config.TTS_AUDIO_BLOCK_MS / 1000)), latency="low", callback=self._callback)
            try:
                self.stream = sd.OutputStream(**arguments, **options)
            except sd.PortAudioError:
                if not options:
                    raise
                check_cancelled(cancellation)
                device = options.get("device", "default")
                if device != _warned_wasapi_device:
                    logging.warning("Shared-mode speech output unavailable; retaining the original audio driver")
                    _warned_wasapi_device = device
                self.stream = sd.OutputStream(**arguments)
                options = {}
            if trace is not None:
                trace.mark("output_opened")
            try:
                self.stream.start()
            except sd.PortAudioError:
                # Never replay samples that a partially started driver consumed.
                if not options or job.offset:
                    raise
                failed_stream, self.stream = self.stream, None
                failed_stream.close()
                check_cancelled(cancellation)
                self.stream = sd.OutputStream(**arguments)
                self.stream.start()
            if trace is not None:
                trace.mark("output_started")
        notified = False
        while True:
            check_cancelled(cancellation)
            if not self.stream.active:
                raise RuntimeError("The speech output device stopped unexpectedly")
            now = time.perf_counter_ns()
            if trace is not None and self._first_callback_ns is not None:
                trace.mark("device_first_callback", self._first_callback_ns)
            if not notified and job.start_ns is not None and now >= job.start_ns:
                notified = True
                if trace is not None:
                    trace.mark("device_submitted", job.submission_ns)
                    trace.mark("device_playback", job.start_ns)
                if on_started:
                    on_started()
            if job.submitted.is_set() and (job.start_ns is None or notified):
                break
            job.submitted.wait(.02 if notified else .005) if not job.submitted.is_set() else time.sleep(.002)
        check_cancelled(cancellation)

    def close(self, abort=False, cancellation=None):
        if self.stream is None:
            return
        try:
            if abort or self.cancelled:
                self.stream.abort()
                mark("output_abort_complete")
            else:
                remaining = (self.last_end_ns - time.perf_counter_ns()) / 1e9
                if remaining > 0:
                    if cancellation is not None:
                        abort = cancellation.wait(min(.5, remaining))
                    else:
                        time.sleep(min(.5, remaining))  # Worker thread; drains the final device buffer.
                if abort:
                    self.stream.abort()
                    mark("output_abort_complete")
                else:
                    self.stream.stop()
        finally:
            self.stream.close()
            self.stream = None
            if self._trace is not None:
                collector = self._trace.collector
                if hasattr(collector, "increment"):
                    collector.increment("output_callbacks", self._callbacks)
                    collector.increment("output_underflows", self._underflows)
                    collector.increment("output_empty_start_callbacks", self._empty_start_callbacks)
                for frames in self._gaps:
                    collector.record("playback_inter_chunk_gap", frames * 1e9 / self.rate)


@contextmanager
def playback_session(cancellation=None):
    if _current.get() is not None:
        yield _current.get()
        return
    while not _device_lock.acquire(timeout=.025):
        check_cancelled(cancellation)
    session = PlaybackSession()
    token = _current.set(session)
    failed = False
    try:
        yield session
    except BaseException:
        failed = True
        raise
    finally:
        try:
            session.close(abort=failed or (cancellation is not None and cancellation.is_set()), cancellation=cancellation)
        finally:
            _current.reset(token)
            _device_lock.release()


def play_samples(samples, rate, cancellation=None, on_started=None):
    with playback_session(cancellation) as session:
        session.play(samples, rate, cancellation, on_started)

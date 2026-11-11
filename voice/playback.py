"""One device stream per reply, reused across native chunks and sentences.

The audio callback only copies PCM and publishes numeric device timing. Python
callbacks, metrics, cleanup and waiting happen on the speech worker thread.
PortAudio's DAC time estimates presentation; it is not an acoustic measurement.
"""

import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock

import config
from core.cancellation import check_cancelled
from core.latency import current_trace

_current = ContextVar("capri_output_stream", default=None)
_device_lock = Lock()


class _Job:
    def __init__(self, samples, cancellation):
        import numpy as np
        self.samples, self.cancellation = samples, cancellation
        self.offset = 0
        self.submitted = Event()
        self.start_ns = None
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

    def _callback(self, output, frames, clock, status):
        output.fill(0)
        position = 0
        now = time.perf_counter_ns()
        dac_ns = now + int(max(0, clock.outputBufferDacTime - clock.currentTime) * 1e9)
        while self.jobs and position < frames:
            job = self.jobs[0]
            if job.cancellation is not None and job.cancellation.is_set():
                self.cancelled = True
                self.jobs.popleft()
                job.submitted.set()
                continue
            count = min(frames - position, len(job.samples) - job.offset)
            output[position:position + count] = job.samples[job.offset:job.offset + count]
            if (job.start_ns is None and job.first_sound is not None
                    and job.offset <= job.first_sound < job.offset + count):
                job.start_ns = dac_ns + int((position + job.first_sound - job.offset) * 1e9 / self.rate)
            job.offset += count
            position += count
            if job.offset == len(job.samples):
                job.end_ns = dac_ns + int(position * 1e9 / self.rate)
                self.last_end_ns = job.end_ns
                self.jobs.popleft()
                job.submitted.set()

    def play(self, samples, rate, cancellation, on_started):
        import sounddevice as sd
        check_cancelled(cancellation)
        if self.stream is None:
            self.rate, self.channels = rate, samples.shape[1]
            self.stream = sd.OutputStream(samplerate=rate, channels=self.channels, dtype="int16",
                blocksize=max(64, int(rate * config.TTS_AUDIO_BLOCK_MS / 1000)),
                latency="low", callback=self._callback)
            self.stream.start()
        elif (rate, samples.shape[1]) != (self.rate, self.channels):
            raise RuntimeError("Speech format changed during a reply")
        job = _Job(samples, cancellation)
        trace = current_trace()
        self.jobs.append(job)
        notified = False
        while True:
            check_cancelled(cancellation)
            if not self.stream.active:
                raise RuntimeError("The speech output device stopped unexpectedly")
            now = time.perf_counter_ns()
            if not notified and job.start_ns is not None and now >= job.start_ns:
                notified = True
                if trace is not None:
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
            else:
                remaining = (self.last_end_ns - time.perf_counter_ns()) / 1e9
                if remaining > 0:
                    if cancellation is not None:
                        abort = cancellation.wait(min(.5, remaining))
                    else:
                        time.sleep(min(.5, remaining))  # Worker thread; drains the final device buffer.
                self.stream.abort() if abort else self.stream.stop()
        finally:
            self.stream.close()
            self.stream = None


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

"""Sentence delivery overlaps local text generation with interruptible speech."""

import re
import time
from contextvars import copy_context
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

from core.cancellation import AnyEvent, check_cancelled
from core.interaction import asks_to_repeat
from core.latency import mark


class StreamingReply:
    def __init__(self, cancellation, interaction, question, on_partial, speak=None,
                 prepare=None, discard=None, min_segment_chars=0, first_chunk_max_chars=0,
                 prepare_stream=None, text_queue_limit=16, first_sentence_min_chars=0):
        self._audio_stop = Event()
        self.cancellation = AnyEvent(cancellation, self._audio_stop)
        self.interaction = interaction
        self.question = question
        self.on_partial = on_partial
        self.speak = speak
        self.prepare = prepare
        self.prepare_stream = prepare_stream
        self.discard = discard or (lambda item: None)
        self._min_chars = max(0, int(min_segment_chars))
        self._first_max = max(0, int(first_chunk_max_chars))
        self._first_sentence_min = max(0, int(first_sentence_min_chars))
        self._submitted = 0
        self._partial_at = 0.0
        self._last_partial = ""
        self.text = ""
        self.pending = ""
        self.error = None
        self._error_lock = Lock()
        self._queue = Queue(maxsize=max(1, int(text_queue_limit)))
        self._prepared = Queue(maxsize=2)
        self._thread = None
        self._producer = None
        if speak:
            if prepare or prepare_stream:
                self._producer = Thread(target=copy_context().run, args=(self._prepare_audio,), name="Capri speech preparation", daemon=True)
                self._producer.start()
            self._thread = Thread(target=copy_context().run, args=(self._audio,), name="Capri streamed speech", daemon=True)
            self._thread.start()

    def feed(self, delta):
        if self.error:
            raise self.error
        check_cancelled(self.cancellation)
        self.text += delta
        self.pending += delta
        now = time.monotonic()
        if not self._last_partial or now - self._partial_at >= .04:
            self.on_partial(self.text)
            self._last_partial, self._partial_at = self.text, now
        while True:
            boundary = None
            for match in re.finditer(r'[.!?](?:["\u201d])?(?=\s)', self.pending):
                prefix = self.pending[:match.end()]
                if "```" in prefix:
                    break  # Let the complete code block be cleaned before speech.
                if re.search(r"\b(?:Mr|Mrs|Dr|e\.g|i\.e)\.$", prefix, re.I):
                    continue
                if self._first_sentence_min and len(prefix.strip()) < self._min_chars:
                    if re.search(r"\b(?:Ms|Prof|Sr|Jr|St|Mt|vs|etc|approx|No|Inc|Ltd)\.$", prefix, re.I):
                        continue
                    if re.search(r"(?:\b[A-Za-z]\.){2,}$|\b[A-Za-z]\.$", prefix):
                        continue
                minimum = self._min_chars
                if not self._submitted and self._first_sentence_min:
                    minimum = min(minimum, self._first_sentence_min)
                if len(prefix.strip()) >= minimum:
                    boundary = match.end()
                    break
            if boundary is None:
                if self._submitted == 0 and self._first_max and "```" not in self.pending:
                    # A complete early clause can sound natural before a long
                    # sentence finishes. Never split decimal/grouped numbers.
                    for match in re.finditer(r"[,;:](?=\s)", self.pending):
                        if self._min_chars <= match.end() <= self._first_max:
                            boundary = match.end()
                            break
                if boundary is None:
                    break
            self._sentence(self.pending[:boundary])
            self.pending = self.pending[boundary:].lstrip()

    def _sentence(self, text):
        if self.speak and text.strip():
            mark("tts_queued")
            self._put(self._queue, text.strip())
            self._submitted += 1

    def _should_speak(self, sentence):
        return (asks_to_repeat(self.question)
                or not self.interaction.repeats_recent_fragment(sentence))

    def _get(self, queue):
        while True:
            check_cancelled(self.cancellation)
            try:
                return queue.get(timeout=.05)
            except Empty:
                continue

    def _put(self, queue, item):
        while True:
            check_cancelled(self.cancellation)
            try:
                queue.put(item, timeout=.025)
                return
            except Full:
                continue

    def _put_prepared(self, item):
        self._put(self._prepared, item)

    def _failed(self, error):
        with self._error_lock:
            if self.error is None:
                self.error = error
        self._audio_stop.set()

    def _prepare_audio(self):
        try:
            while (sentence := self._get(self._queue)) is not None:
                if not self._should_speak(sentence):
                    continue
                if self.prepare_stream is not None:
                    first = True
                    def ready(prepared):
                        nonlocal first
                        try:
                            self._put_prepared((sentence, prepared, not first))
                            first = False
                        except BaseException:
                            self.discard(prepared)
                            raise
                    self.prepare_stream(sentence, ready)
                    continue
                prepared = self.prepare(sentence)
                if prepared is None:
                    continue
                try:
                    self._put_prepared((sentence, prepared, False))
                except BaseException:
                    self.discard(prepared)
                    raise
            self._put_prepared(None)
        except BaseException as error:
            self._failed(error)

    def _audio(self):
        from voice.playback import playback_session
        try:
            with playback_session(self.cancellation):
                self._play_audio()
        except BaseException as error:
            self._failed(error)

    def _play_audio(self):
        try:
            if self.prepare or self.prepare_stream:
                skip = False
                while (item := self._get(self._prepared)) is not None:
                    sentence, prepared, continuation = item
                    try:
                        # A previous segment may have finished since preparation.
                        if not continuation:
                            skip = not self._should_speak(sentence)
                        if not skip:
                            check_cancelled(self.cancellation)
                            self.speak(prepared)
                    finally:
                        self.discard(prepared)
            else:
                while (sentence := self._get(self._queue)) is not None:
                    if self._should_speak(sentence):
                        self.speak(sentence)
        except BaseException as error:
            self._failed(error)

    def _join(self):
        for thread in (self._producer, self._thread):
            if thread:
                while thread.is_alive():
                    thread.join(.05)
        while True:
            try:
                item = self._prepared.get_nowait()
            except Empty:
                break
            if item is not None:
                try:
                    self.discard(item[1])
                except BaseException as error:
                    self._failed(error)

    def finish(self):
        if self.pending.count("```") % 2:
            self.pending = self.pending.split("```", 1)[0] + " Code is shown in chat."
        self._sentence(self.pending)
        self.pending = ""
        if self.text != self._last_partial:
            self.on_partial(self.text)
            self._last_partial = self.text
        self._put(self._queue, None)
        self._join()
        if self.error:
            raise self.error
        check_cancelled(self.cancellation)

    def abort(self):
        self._audio_stop.set()
        self._join()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Empty:
                break

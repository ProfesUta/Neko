"""Sentence delivery overlaps local text generation with interruptible speech."""

import re
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

from core.cancellation import AnyEvent, check_cancelled
from core.interaction import asks_to_repeat


class StreamingReply:
    def __init__(self, cancellation, interaction, question, on_partial, speak=None,
                 prepare=None, discard=None, min_segment_chars=0):
        self._audio_stop = Event()
        self.cancellation = AnyEvent(cancellation, self._audio_stop)
        self.interaction = interaction
        self.question = question
        self.on_partial = on_partial
        self.speak = speak
        self.prepare = prepare
        self.discard = discard or (lambda item: None)
        self._min_chars = max(0, int(min_segment_chars))
        self.text = ""
        self.pending = ""
        self.error = None
        self._error_lock = Lock()
        self._queue = Queue()
        self._prepared = Queue(maxsize=2)
        self._thread = None
        self._producer = None
        if speak:
            if prepare:
                self._producer = Thread(target=self._prepare_audio, name="Capri speech preparation", daemon=True)
                self._producer.start()
            self._thread = Thread(target=self._audio, name="Capri streamed speech", daemon=True)
            self._thread.start()

    def feed(self, delta):
        if self.error:
            raise self.error
        check_cancelled(self.cancellation)
        self.text += delta
        self.pending += delta
        self.on_partial(self.text)
        while True:
            boundary = None
            for match in re.finditer(r'[.!?](?:["\u201d])?(?=\s)', self.pending):
                prefix = self.pending[:match.end()]
                if "```" in prefix:
                    break  # Let the complete code block be cleaned before speech.
                if re.search(r"\b(?:Mr|Mrs|Dr|e\.g|i\.e)\.$", prefix, re.I):
                    continue
                if len(prefix.strip()) >= self._min_chars:
                    boundary = match.end()
                    break
            if boundary is None:
                break
            self._sentence(self.pending[:boundary])
            self.pending = self.pending[boundary:].lstrip()

    def _sentence(self, text):
        if self.speak and text.strip():
            self._queue.put(text.strip())

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

    def _put_prepared(self, item):
        while True:
            check_cancelled(self.cancellation)
            try:
                self._prepared.put(item, timeout=.05)
                return
            except Full:
                continue

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
                prepared = self.prepare(sentence)
                if prepared is None:
                    continue
                try:
                    self._put_prepared((sentence, prepared))
                except BaseException:
                    self.discard(prepared)
                    raise
            self._put_prepared(None)
        except BaseException as error:
            self._failed(error)

    def _audio(self):
        try:
            if self.prepare:
                while (item := self._get(self._prepared)) is not None:
                    sentence, prepared = item
                    try:
                        # A previous segment may have finished since preparation.
                        if self._should_speak(sentence):
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
        self._queue.put(None)
        self._join()
        if self.error:
            raise self.error
        check_cancelled(self.cancellation)

    def abort(self):
        self._audio_stop.set()
        self._queue.put(None)
        self._join()

"""Sentence delivery overlaps local text generation with interruptible speech."""

import re
from queue import Queue
from threading import Event, Thread

from core.cancellation import AnyEvent, check_cancelled
from core.interaction import asks_to_repeat


class StreamingReply:
    def __init__(self, cancellation, interaction, question, on_partial, speak=None):
        self._audio_stop = Event()
        self.cancellation = AnyEvent(cancellation, self._audio_stop)
        self.interaction = interaction
        self.question = question
        self.on_partial = on_partial
        self.speak = speak
        self.text = ""
        self.pending = ""
        self.error = None
        self._queue = Queue()
        self._thread = None
        if speak:
            self._thread = Thread(target=self._audio, name="Capri streamed speech", daemon=True)
            self._thread.start()

    def feed(self, delta):
        check_cancelled(self.cancellation)
        self.text += delta
        self.pending += delta
        self.on_partial(self.text)
        while match := re.search(r'[.!?](?:["\u201d])?(?=\s)', self.pending):
            boundary = match.end()
            # Avoid cutting decimal numbers and common abbreviations.
            prefix = self.pending[:boundary]
            if "```" in prefix:
                break  # Let the complete code block be cleaned before speech.
            if re.search(r"\b(?:Mr|Mrs|Dr|e\.g|i\.e)\.$", prefix, re.I):
                break
            self._sentence(prefix)
            self.pending = self.pending[boundary:].lstrip()

    def _sentence(self, text):
        if self.speak and text.strip():
            self._queue.put(text.strip())

    def _audio(self):
        try:
            while (sentence := self._queue.get()) is not None:
                check_cancelled(self.cancellation)
                if (not asks_to_repeat(self.question)
                        and self.interaction.repeats_recent_fragment(sentence)):
                    continue
                self.speak(sentence)
        except BaseException as error:
            self.error = error

    def finish(self):
        if self.pending.count("```") % 2:
            self.pending = self.pending.split("```", 1)[0] + " Code is shown in chat."
        self._sentence(self.pending)
        self.pending = ""
        self._queue.put(None)
        if self._thread:
            while self._thread.is_alive():
                self._thread.join(0.05)
            check_cancelled(self.cancellation)
        if self.error:
            raise self.error

    def abort(self):
        self._audio_stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join()

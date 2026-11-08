"""Thread-safe conversation priority and recent speech feedback guards."""

import re
import time
from collections import deque
from difflib import SequenceMatcher
from threading import RLock

from config import (
    ADVICE_REPEAT_SECONDS, VOICE_CONVERSATION_GRACE_SECONDS,
    VOICE_ECHO_WINDOW_SECONDS, VOICE_OUTPUT_TAIL_SECONDS, VOICE_REPLY_REPEAT_SECONDS,
    VOICE_SHORT_ECHO_WINDOW_SECONDS,
)


def normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))


def asks_to_repeat(text):
    text = normalize(text)
    if re.search(r"\b(?:don t|dont|do not|stop|never|quit)\b.*\b(?:say|tell|read|repeat|repeating)\b", text):
        return False
    return bool(
        text in ("again", "once more", "one more time")
        or re.search(r"\b(?:say|tell|read)\b.*\b(?:again|once more)\b", text)
        or re.match(r"(?:please )?repeat (?:that|it|your|the|what)", text)
    )


def _long_enough(text):
    return len(text) >= 18 and len(text.split()) >= 4


def _same_text(first, second, allow_excerpt=False):
    if not _long_enough(first):
        return False
    if first == second:
        return True
    if allow_excerpt and first in second:
        return True
    return SequenceMatcher(None, first, second).ratio() >= (0.86 if allow_excerpt else 0.96)


class InteractionState:
    def __init__(self):
        self._lock = RLock()
        self._user_active = False
        self._last_user_activity = float("-inf")
        self._last_playback_end = float("-inf")
        self._spoken = deque(maxlen=8)
        self._answers = deque(maxlen=8)

    def begin_user_turn(self):
        with self._lock:
            self._user_active = True
            self._last_user_activity = time.monotonic()

    def finish_user_turn(self):
        with self._lock:
            if self._user_active:
                self._last_user_activity = time.monotonic()
            self._user_active = False

    def allows_advice(self, captured_at):
        with self._lock:
            return (
                not self._user_active
                and time.monotonic() - self._last_user_activity >= VOICE_CONVERSATION_GRACE_SECONDS
                and captured_at >= self._last_user_activity
            )

    def record_spoken(self, text):
        with self._lock:
            now = time.monotonic()
            self._spoken.append((now, normalize(text)))
            self._last_playback_end = now

    def microphone_delay(self):
        with self._lock:
            return max(0, self._last_playback_end + VOICE_OUTPUT_TAIL_SECONDS - time.monotonic())

    def is_probable_echo(self, text, captured_at=None):
        text = normalize(text)
        # A user challenging/quoting Capri must still get a turn.
        if re.match(r"(?:why (?:did|are|do) you|what do you mean|you said)\b", text):
            return False
        with self._lock:
            now = time.monotonic() if captured_at is None else captured_at
            return any(
                (
                    0 <= now - when <= VOICE_ECHO_WINDOW_SECONDS
                    and _same_text(text, spoken, allow_excerpt=True)
                ) or (
                    0 <= now - when <= VOICE_SHORT_ECHO_WINDOW_SECONDS
                    and len(text) >= 2 and text == spoken
                )
                for when, spoken in self._spoken
            )

    def accept_answer(self, answer, question, suppress_duplicates=False):
        normalized = normalize(answer)
        with self._lock:
            now = time.monotonic()
            if suppress_duplicates and not asks_to_repeat(question) and any(
                now - when <= VOICE_REPLY_REPEAT_SECONDS and _same_text(normalized, previous)
                for when, previous in self._answers
            ):
                return False
            self._answers.append((now, normalized))
            return True

    def repeats_recent_response(self, text):
        text = normalize(text)
        with self._lock:
            now = time.monotonic()
            return any(
                now - when <= ADVICE_REPEAT_SECONDS and _same_text(text, previous)
                for when, previous in (*self._answers, *self._spoken)
            )

    def repeats_recent_fragment(self, text):
        text = normalize(text)
        with self._lock:
            now = time.monotonic()
            return any(now - when <= VOICE_REPLY_REPEAT_SECONDS
                       and _same_text(text, previous, allow_excerpt=True)
                       for when, previous in (*self._answers, *self._spoken))

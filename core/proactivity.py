"""Evidence requirements and duplicate suppression for unsolicited advice."""

import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

from config import (
    ADVICE_ATTEMPT_COOLDOWN_SECONDS, ADVICE_MAX_AGE_SECONDS,
    ADVICE_MIN_GAP_SECONDS, ADVICE_REPEAT_SECONDS,
)


@dataclass(frozen=True)
class ScreenAdvice:
    text: str
    event_key: str
    evidence: str
    source: str
    captured_at: float

    def is_fresh(self):
        return 0 <= time.monotonic() - self.captured_at <= ADVICE_MAX_AGE_SECONDS


@dataclass(frozen=True)
class ScreenReading:
    text: str
    captured_at: float
    direct_answer: str | None = None

    def __iter__(self):
        yield self.text
        yield self.captured_at


def direct_screen_answer(content, question):
    """Do not shortcut explicit action/memory requests into an explanation."""
    command = re.compile(
        r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
        r"(?:open|close|launch|start|stop|search|find|list|remember|forget)\b",
        re.I,
    )
    if command.search(question):
        return None
    if re.search(
        r"\b(?:stop repeating|keep repeating|keep saying|listen to me|are you listening|"
        r"what did (?:i|you) (?:say|ask)|i asked you|different question|stop saying|"
        r"let me talk|let me speak|be quiet|(?:don't|do not) say that again)\b", question, re.I,
    ):
        return None
    data = json.loads(content)
    answer = data.get("question_answer")
    if (
        data.get("answer_directly") is True and data.get("requires_tools") is False
        and isinstance(answer, str) and 0 < len(answer.strip()) <= 2000
    ):
        return answer.strip()
    return None


class AdvicePolicy:
    def __init__(self):
        self._recent = []
        self._attempts = []
        self._last_delivered = float("-inf")
        self._snoozed_until = float("-inf")

    @staticmethod
    def _normalize(text):
        return " ".join("".join(
            char for char in text.lower() if char.isalpha() or char.isspace()
        ).split())

    def _same_issue(self, previous, advice):
        old_text, new_text = self._normalize(previous.text), self._normalize(advice.text)
        # Switching window/monitor IDs must not bypass identical-sentence suppression.
        if old_text == new_text:
            return True
        if previous.source != advice.source:
            return False
        same_key = self._normalize(previous.event_key) == self._normalize(advice.event_key)
        old_evidence, evidence = self._normalize(previous.evidence), self._normalize(advice.evidence)
        same_evidence = len(evidence) >= 5 and evidence == old_evidence
        similar_tip = SequenceMatcher(None, old_text, new_text).ratio() > 0.75
        return same_key or same_evidence or similar_tip

    def should_offer(self, advice):
        now = time.monotonic()
        if not advice.is_fresh() or now < self._snoozed_until:
            return False
        if now - self._last_delivered < ADVICE_MIN_GAP_SECONDS:
            return False
        self._recent = [item for item in self._recent if now - item[0] < ADVICE_REPEAT_SECONDS]
        self._attempts = [item for item in self._attempts if now - item[0] < ADVICE_ATTEMPT_COOLDOWN_SECONDS]
        for _when, previous in (*self._recent, *self._attempts):
            if self._same_issue(previous, advice):
                return False
        return True

    def reserve(self, advice):
        # Failed/expired/cancelled tips must not keep stealing the microphone.
        self._attempts.append((time.monotonic(), advice))
        self._attempts = self._attempts[-20:]

    def record_delivery(self, advice):
        self._last_delivered = time.monotonic()
        self._recent.append((self._last_delivered, advice))
        self._recent = self._recent[-20:]

    def snooze(self, seconds=15):
        self._snoozed_until = time.monotonic() + seconds


ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "activity": {"type": "string"},
        "visible_details": {"type": "array", "items": {"type": "string"}},
        "question_answer": {"type": "string"},
        "answer_directly": {"type": "boolean"},
        "requires_tools": {"type": "boolean"},
        "uncertainty": {"type": "string"},
        "sensitive": {"type": "boolean"},
        "urgent_advice": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "needed_now": {"type": "boolean"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "event_key": {"type": "string"},
                        "evidence": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["needed_now", "confidence", "event_key", "evidence", "text"],
                    "additionalProperties": False,
                },
            ],
        },
    },
    "required": [
        "summary", "activity", "visible_details", "question_answer", "answer_directly",
        "requires_tools", "uncertainty",
        "sensitive", "urgent_advice",
    ],
    "additionalProperties": False,
}


def parse_assessment(
    content, source, captured_at, allow_advice=False, source_id=None,
    include_question_answer=True,
):
    """Malformed or uncertain outputs can never become unsolicited speech."""
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        raise ValueError("The vision model returned an invalid screen description.")
    parts = [f"Application: {source}", data["summary"][:1200]]
    details = data.get("visible_details", [])
    if isinstance(details, list):
        parts.extend(str(detail)[:250] for detail in details[:8] if isinstance(detail, str))
    for key, label in (("question_answer", "Question evidence"), ("uncertainty", "Uncertainty")):
        if key == "question_answer" and not include_question_answer:
            continue
        if isinstance(data.get(key), str) and data[key].strip():
            parts.append(f"{label}: {data[key][:1400]}")
    advice = None
    candidate = data.get("urgent_advice")
    if (
        allow_advice and data.get("sensitive") is False and isinstance(candidate, dict)
        and candidate.get("needed_now") is True and candidate.get("confidence") == "high"
        and all(isinstance(candidate.get(key), str) and candidate[key].strip()
                for key in ("text", "evidence", "event_key"))
    ):
        advice = ScreenAdvice(
            candidate["text"].strip()[:280], candidate["event_key"].strip()[:100],
            candidate["evidence"].strip()[:400], source_id or source, captured_at,
        )
    return "\n".join(parts)[:4500], advice

"""Conservative response gates: uncertainty is data, not permission to guess."""

import json
import re
import time


CLARIFY = "I'm not sure what you mean. Which champion, item, or part of the screen are you asking about?"
UNVERIFIED = "I couldn't verify that from the League API and screen. What specific situation should I check?"


def is_proactive(message):
    return bool(message.get("proactive") or str(message.get("content", "")).startswith(
        "[Unprompted advice from a screen snapshot]"))


def normalize(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def information_request(text):
    return bool("?" in text or re.search(
        r"\b(?:how|why|what|which|where|who|when|should i|can i|could i|tell me|explain|help me|give me|any advice)\b", text, re.I))


def relevant_notice(text, messages):
    if not re.search(r"\b(?:what do you mean|why did you|you said|that (?:tip|warning|notice)|what did you)\b", text, re.I):
        return None
    for item in reversed(messages):
        captured = item.get("captured_at")
        if is_proactive(item) and type(captured) in (int, float) and 0 <= time.monotonic() - captured <= 20:
            return {"text": item["content"], "source": item.get("source"), "evidence": item.get("evidence", "")}
    return None


def has_observed_evidence(evidence, assessment):
    evidence = normalize(evidence)
    details = assessment.get("visible_details", [])
    details = details if isinstance(details, list) else []
    observations = [assessment.get("summary", ""), *details]
    return len(evidence) >= 4 and any(evidence in normalize(item) for item in observations)


def league_screen_answer(content):
    data = json.loads(content)
    if not isinstance(data, dict):
        return UNVERIFIED
    if data.get("question_understood") is not True:
        return CLARIFY
    evidence = data.get("answer_evidence")
    if (data.get("answer_verified") is not True or data.get("sensitive") is not False
            or data.get("requires_tools") is not False or str(data.get("uncertainty", "")).strip()
            or not isinstance(evidence, list) or not evidence
            or not all(has_observed_evidence(item, data) for item in evidence)):
        return UNVERIFIED
    answer = data.get("question_answer")
    if not isinstance(answer, str) or not 0 < len(answer.strip()) <= 1200:
        return UNVERIFIED
    return answer.strip()


def ambiguous_question(text, messages):
    """Clarify a bare reference without usable dialogue or visual context."""
    normalized = normalize(text).strip(" .?!")
    if not re.fullmatch(r"(?:what about (?:it|that|this)|and (?:that|this)|which one|what do you mean|do (?:it|that))", normalized):
        return None
    if relevant_notice(text, messages):
        return None
    previous = [item for item in messages[:-1] if item.get("role") in ("user", "assistant") and not is_proactive(item)]
    if not previous:
        return "I'm not sure what you're referring to. What would you like me to check?"
    return None

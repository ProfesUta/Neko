"""Small delivery cues for existing speech/poses, without an extra model call."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyDelivery:
    style: str
    expression: str | None = None


def reply_delivery(question, answer, urgent=False):
    if urgent:
        return ReplyDelivery("urgent")
    question = question.casefold().replace("\u2019", "'")
    answer = answer.casefold().replace("\u2019", "'")
    if re.search(r"\b(frustrated|stressed|upset|sad|overwhelmed|scared|anxious|bad day|"
                 r"stop (?:joking|teasing)|be serious|not funny)\b", question):
        return ReplyDelivery("warm")
    if re.search(r"\b(error|failed|unavailable|missing|can't|cannot|couldn't|not found|haven't verified)\b", answer):
        return ReplyDelivery("focused")
    if re.search(r"\b(thanks|thank you|good job|nice work|that worked|(?:we|i) (?:finally )?(?:won|did it)|nailed it)\b", question):
        return ReplyDelivery("happy", "happy")
    if re.search(r"\b(joke|tease me|roast me|haha|that's funny|troublemaker)\b", question):
        return ReplyDelivery("playful", "happy")
    if "```" in answer or re.search(r"\b(debug|traceback|explain|calculate|where is|where's)\b", question):
        return ReplyDelivery("focused")
    return ReplyDelivery("playful")

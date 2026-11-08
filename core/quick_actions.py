"""Conservative parsing for screen-location commands that need no LLM planning."""

import re
from dataclasses import dataclass

from core.spatial import monitor_reference


APP_NAMES = {
    "discord": "Discord", "slack": "Slack", "browser": "browser",
    "chrome": "Chrome", "google chrome": "Chrome", "firefox": "Firefox",
    "mozilla firefox": "Firefox", "edge": "Edge", "microsoft edge": "Edge",
    "explorer": "Explorer", "file explorer": "Explorer", "notepad": "Notepad",
    "calculator": "Calculator", "spotify": "Spotify", "steam": "Steam",
    "terminal": "Terminal", "vscode": "VSCode", "vs code": "VSCode",
    "visual studio code": "VSCode",
}


@dataclass(frozen=True)
class LocationRequest:
    query: str
    monitor_id: str | None = None


def parse_location_request(text):
    text = text.casefold().replace("\u2019", "'").strip(" \t\n.!?,")
    # Match an entire simple command. Compound questions stay with the model.
    text = re.sub(r"^(?:(?:hey\s+)?capri[, ]+|please\s+|(?:can|could|would) you\s+)+", "", text)
    match = re.fullmatch(
        r"(?:where(?:'s| is| are)|where can i find|show me(?: where)?|find|locate|point (?:at|to))\s+(.+)", text)
    if not match:
        return None
    body = re.sub(r"(?:[, ]+(?:please|right now|now|for me))+$", "", match[1]).strip()
    reference = monitor_reference(body)
    suffix = re.search(
        r"\s+on (?:my |the )?(?:(?:first|second|third|fourth) (?:monitor|screen|display)"
        r"|(?:monitor|screen|display)(?:\s*[1-9]\d*)?|desktop)$", body)
    explicit_screen = suffix is not None
    if suffix:
        body = body[:suffix.start()].strip()
    body = re.sub(r"\s+(?:is|are)$", "", body)
    body = re.sub(r"^(?:my |the |a )", "", body).strip()
    if (not body or len(body) > 100 or re.search(r"[?!;:]|\b(?:and|or|why|how|when|what|then|don't|not)\b", body)):
        return None
    app = re.sub(r"\s+(?:app|application|aplication|window)$", "", body)
    if app in APP_NAMES:
        return LocationRequest(APP_NAMES[app], reference)
    # Bare 'where is Paris?' remains a general knowledge question.
    if explicit_screen or re.search(r"\b(?:button|control|icon|menu|toggle|window|app|application)$", body):
        return LocationRequest(body, reference)
    return None


def location_reply(result, request, variant=0):
    status = result.get("status")
    if status == "pointing":
        monitor = result["monitor_number"]
        return (
            f"Right there on monitor {monitor}, chum.",
            f"Found it on monitor {monitor}. Follow the red lightning.",
            f"There it is, monitor {monitor}. I've marked it for you.",
        )[variant % 3]
    if status == "ambiguous":
        return result.get("message", "Which window or control do you mean, chum?")
    if status == "not_found":
        if result.get("native_status"):
            return "I can't read the visible app windows in this desktop session right now."
        return f"I can't find a visible {request.query} here. Is it minimized or covered, chum?"
    if status in ("invalid_monitor", "unavailable", "disabled"):
        return result["message"]
    return "I can't verify the exact spot yet, chum. Tell me which app or control you mean."

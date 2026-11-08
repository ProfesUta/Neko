"""Verified spatial targets and cancellable worker-to-GUI actions.

The model chooses labels and opaque target IDs. Only native/accessibility/OCR
evidence can create an actionable rectangle; model coordinates are never used.
"""

import re
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from threading import Event, Lock

from core.cancellation import AnyEvent, OperationCancelled, check_cancelled
from core.monitors import normalize_monitor_reference
from core.spatial_geometry import Rect
from core.window_grounding import visible_parts
from config import SPATIAL_TARGET_TTL_SECONDS


class MovementIntent(str, Enum):
    IDLE = "idle"
    AUTONOMOUS = "autonomous"
    LOCATE = "locate"
    POINT = "point"
    ASSIST = "assist"
    RETURN = "return"


class ActionState(str, Enum):
    IDLE = "idle"
    SEARCHING = "searching"
    TARGET_FOUND = "target_found"
    TELEPORTING = "teleporting"
    POINTING = "pointing"
    SPEAKING = "speaking"


class TargetUnavailable(ValueError):
    """Expected target lifecycle change, not a failed tool implementation."""


def spatial_request(text):
    text = text.casefold().replace("’", "'")
    return bool(re.search(r"\b(where(?:'s| is| are)|find|locate|point (?:at|to)|show me|take me back|return to)\b", text)
                or (re.search(r"\b(icon|button|control)\b", text) and re.search(r"\b(monitor|find|locate)\b", text)))


def monitor_reference(text):
    match = re.search(r"\b(?:monitor|screen|display)\s*([1-9]\d*)\b", text, re.I)
    if match:
        return match[1]
    match = re.search(r"\b(first|second|third|fourth)\s+(?:monitor|screen|display)\b", text, re.I)
    return {"first": "1", "second": "2", "third": "3", "fourth": "4"}.get(match[1].lower()) if match else None


def query_words(text):
    text = re.sub(r"\b(?:monitor|screen|display)\s*\d+\b", " ", text.casefold())
    ignored = set("capri where s is are my me the a an please show find locate point at to on of what that this it"
                  " first second third fourth monitor screen display button control icon window app application".split())
    return tuple(word for word in re.findall(r"[\w]+", text) if word not in ignored)


def match_label(query, label, application=""):
    words = query_words(query)
    if not words:
        return 0.0
    label_words = set(re.findall(r"[\w]+", label.casefold()))
    app_words = set(re.findall(r"[\w]+", application.casefold()))
    aliases = {"browser": {"chrome", "msedge", "firefox", "brave", "opera"},
               "edge": {"msedge"}, "vscode": {"code", "vscodium"},
               "discord": {"discord", "discordcanary", "discordptb"},
               "explorer": {"explorer"}, "settings": {"systemsettings"}}
    if len(words) == 1 and aliases.get(words[0], set()) & app_words:
        return 1.0
    if set(words) <= app_words:
        return 1.0
    if set(words) == label_words:
        return 0.99
    if set(words) <= label_words:
        return 0.9
    return 0.0


@dataclass(frozen=True)
class SpatialTarget:
    id: str
    monitor_id: str
    target_type: str
    label: str
    bounding_box: Rect  # Qt logical global coordinates.
    physical_box: Rect
    confidence: float
    source: str
    timestamp: float
    revision: int
    window_handle: int = 0
    window_pid: int = 0
    window_bounds: Rect | None = None
    runtime_id: str = ""
    query: str = ""

    def to_dict(self):
        return {"target_id": self.id, "monitor_id": self.monitor_id, "target_type": self.target_type,
                "label": self.label, "bounding_box": self.bounding_box.to_dict(),
                "physical_box": self.physical_box.to_dict(), "global_position": self.bounding_box.center,
                "confidence": self.confidence, "source": self.source, "timestamp": self.timestamp,
                "coordinate_space": "Qt logical global pixels"}


@dataclass
class ActionTicket:
    kind: str
    generation: int
    cancellation: object
    target: SpatialTarget | None = None
    duration_ms: int | None = None
    done: Event = field(default_factory=Event)
    result: dict = field(default_factory=dict)


class SpatialService:
    def __init__(self, monitors, grounder, reader, enabled, dispatch):
        self.monitors = monitors
        self.grounder = grounder
        self.reader = reader
        self.enabled = enabled
        self.dispatch = dispatch
        self._lock = Lock()
        self._generation = 0
        self._turn_cancel = Event()
        self._targets = {}
        self._shown = None
        self._last_target = None
        self._requested_spatial = False
        self._requested_monitor = None
        self._invalidated = False

    def begin_turn(self, text, cancel_event=None):
        with self._lock:
            self._turn_cancel.set()
            self._generation += 1
            self._turn_cancel = Event()
            self._shown = None
            self._requested_spatial = spatial_request(text)
            self._requested_monitor = monitor_reference(text) if self._requested_spatial else None
            self._invalidated = False
            generation = self._generation
            cancellation = AnyEvent(self._turn_cancel, cancel_event or Event())
        self.dispatch(ActionTicket("search" if spatial_request(text) else "cancel", generation, cancellation))
        return generation

    def _ticket(self, kind, cancel_event=None, target=None, duration_ms=None):
        with self._lock:
            return ActionTicket(kind, self._generation, AnyEvent(self._turn_cancel, cancel_event or Event()),
                                target, duration_ms)

    def cancel(self):
        with self._lock:
            self._turn_cancel.set()
            self._invalidated = self._invalidated or self._shown is not None
            self._shown = None
            generation = self._generation
        self.dispatch(ActionTicket("cancel", generation, Event()))

    def finish_turn(self, generation):
        with self._lock:
            if generation != self._generation:
                return
            ticket = ActionTicket("finish", generation, self._turn_cancel)
        self.dispatch(ticket)

    def playback_started(self, generation):
        with self._lock:
            if generation != self._generation:
                return
            ticket = ActionTicket("speaking", generation, self._turn_cancel)
        self.dispatch(ticket)

    def _require_enabled(self):
        if not self.enabled():
            raise ValueError("Screen awareness is off. Turn VISION ON before locating or pointing at screen content.")

    def get_monitors(self):
        return {"monitors": [monitor.to_dict() for monitor in self.monitors.snapshot()],
                "topology_revision": self.monitors.revision,
                "selection": "Monitor numbers start at 1. Use an exact ID or number; omit monitor_id to search all displays. 0/auto/all mean all displays, not the primary."}

    def get_visible_windows(self, cancel_event=None):
        self._require_enabled()
        monitors = self.monitors.snapshot()
        windows = self.grounder.windows(cancel_event)
        result = []
        for window in windows:
            parts = visible_parts(window.bounds, window, windows)
            associations = [monitor.id for monitor in monitors if any(monitor.physical.intersection(p).area for p in parts)]
            if associations and window.title:
                result.append({**window.to_dict(), "monitor_ids": associations})
        return {"windows": result, "supported": self.grounder.supported,
                "available": not bool(getattr(self.grounder, "last_error", "")),
                "error": getattr(self.grounder, "last_error", ""),
                "note": "Visible, non-minimized windows; bounds are physical desktop pixels. No apps were activated."}

    def _register(self, label, bounds, source, confidence, window=None, runtime_id="", query="", monitor=None,
                  observed_at=None):
        if not bounds.area or not all(math.isfinite(value) for value in (bounds.x, bounds.y, bounds.width, bounds.height)):
            return None
        revision = self.monitors.revision
        monitors = self.monitors.snapshot()
        if monitor is None:
            monitor = max(monitors, key=lambda item: item.physical.intersection(bounds).area, default=None)
        if monitor is None or not monitor.physical.intersection(bounds).area:
            return None
        physical = bounds.intersection(monitor.physical)
        if physical.width < 3 or physical.height < 3:
            return None
        target = SpatialTarget(
            uuid.uuid4().hex, monitor.id, "window" if source == "window" else "label" if source == "ocr" else "control", label,
            monitor.physical_to_logical(physical), physical, confidence, source,
            time.monotonic() if observed_at is None else observed_at,
            revision, window.handle if window else 0, window.pid if window else 0,
            window.bounds if window else None, runtime_id, query,
        )
        with self._lock:
            self._targets[target.id] = target
            if len(self._targets) > 64:
                self._targets.pop(next(iter(self._targets)))
        return target

    def _find(self, query, monitor, cancel_event, *, previous=None, visual=True):
        windows = self.grounder.windows(cancel_event)
        windows_on_monitor = [window for window in windows
                              if (monitor is None or window.bounds.intersection(monitor.physical).area)
                              and visible_parts(window.bounds, window, windows)]
        scored = []
        control_query = bool(re.search(r"\b(button|control|icon|menu|toggle)\b", query, re.I))
        named_apps = {"discord", "slack", "browser", "chrome", "firefox", "edge", "msedge", "brave",
                      "opera", "notepad", "calculator", "explorer", "terminal", "spotify", "steam", "vscode"}
        words = query_words(query)
        window_query = not control_query and (previous is None or previous.source == "window") and (
            (len(words) == 1 and words[0] in named_apps)
            or bool(re.search(r"\b(app|application|window)\b", query, re.I))
            or (previous is not None and previous.source == "window")
        )
        for window in windows_on_monitor:
            if window.is_desktop:
                continue
            if previous and (window.handle != previous.window_handle or window.pid != previous.window_pid):
                continue
            score = match_label(query, window.title, window.application)
            if score < (0.99 if window_query else 0.9) or control_query or (previous and previous.source != "window"):
                continue
            pieces = visible_parts(window.bounds, window, windows)
            if monitor is not None:
                pieces = [piece.intersection(monitor.physical) for piece in pieces]
            if pieces and max(piece.area for piece in pieces) >= 100:
                scored.append((score, window, max(pieces, key=lambda item: item.area)))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.04:
                return {"status": "ambiguous", "message": "Which visible window do you mean?", "choices": [item[1].title for item in scored[:6]]}
            score, window, bounds = scored[0]
            target = self._register(window.title, bounds, "window", score, window, query=query, monitor=monitor)
            return {"status": "found", "target": target.to_dict()} if target else None
        if window_query:
            return {"status": "not_found", "message": "No confident visible application window matches. Show the app first, or specify its icon/control if that is what you mean.",
                    "native_status": getattr(self.grounder, "last_error", "")}
        parents = [window for window in windows_on_monitor if not previous or (
            window.handle == previous.window_handle and window.pid == previous.window_pid)]
        elements = self.grounder.elements(parents, cancel_event, runtime_id=previous.runtime_id if previous else "")
        matches = []
        for item in elements:
            check_cancelled(cancel_event)
            parent = next((window for window in windows if str(window.handle) == str(item.get("handle"))), None)
            if parent is None or (previous and (parent.handle != previous.window_handle
                                                or parent.pid != previous.window_pid)):
                continue
            if previous and previous.runtime_id and item.get("runtime_id") != previous.runtime_id:
                continue
            score = match_label(query, item.get("name", ""))
            if score < 0.9:
                continue
            if control_query and item.get("control_type", "").endswith(("Window", "Document", "Pane")):
                continue
            try:
                rect = Rect(*(float(item[key]) for key in ("x", "y", "width", "height")))
            except (KeyError, ValueError, TypeError):
                continue
            parts = visible_parts(rect, parent, windows)
            if monitor is not None:
                parts = [part.intersection(monitor.physical) for part in parts]
            if not parts:
                continue
            bounds = max(parts, key=lambda part: part.area)
            if bounds.area < rect.area * 0.85:
                continue  # Do not point at a mostly covered or clipped control.
            matches.append((score, item, parent, bounds))
        if matches:
            matches.sort(key=lambda item: item[0], reverse=True)
            # Some providers expose both a Button and its same-label Text child.
            unique = []
            for match in matches:
                if not any(old[2].handle == match[2].handle and old[3].intersection(match[3]).area >= min(old[3].area, match[3].area) * 0.8 for old in unique):
                    unique.append(match)
            if len(unique) > 1 and unique[0][0] - unique[1][0] < 0.04:
                return {"status": "ambiguous", "message": "Several matching controls are visible. Which one?", "choices": [item[1]["name"] for item in unique[:6]]}
            score, item, parent, bounds = unique[0]
            target = self._register(item["name"], bounds, "accessibility", score, parent,
                                    item.get("runtime_id", ""), query, monitor, item.get("observed_at"))
            return {"status": "found", "target": target.to_dict()} if target else None
        if visual and self.reader() is not None and hasattr(self.reader(), "locate_visual"):
            hint, detections = self.reader().locate_visual(query, monitor.id if monitor else None, cancel_event)
            check_cancelled(cancel_event)
            if len(detections) > 1:
                return {"status": "ambiguous", "message": "Several matching labels are visible. Which monitor or app do you mean?",
                        "choices": [{"label": item["label"], "monitor_id": item["monitor_id"]} for item in detections[:6]]}
            for detection in detections:
                target = self._register(detection["label"], detection["bounds"], "ocr", detection["confidence"], query=query,
                                        monitor=self.monitors.resolve(detection["monitor_id"]))
                if target:
                    return {"status": "found", "target": target.to_dict()}
            if hint.get("label") and match_label(query, hint["label"]) == 0 and hint.get("confidence", 0) >= 0.9:
                hinted_monitor = self.monitors.resolve(hint.get("monitor_id"))
                verified = self._find(hint["label"], hinted_monitor, cancel_event, visual=False)
                if verified:
                    return verified
            return {"status": "unverified", "message": "I cannot verify an exact visible target, so I have not moved or pointed.",
                    "visual_hint": hint, "clarification": "Name the app/control or describe which icon you mean."}
        return {"status": "not_found", "message": "No confident visible match. It may be hidden, minimized, or unavailable to accessibility. I have not moved."}

    def locate_on_screen(self, query, monitor_id=None, cancel_event=None):
        self._require_enabled()
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("Provide a short object/app/control name")
        ticket = self._ticket("search", cancel_event)
        check_cancelled(ticket.cancellation)
        with self._lock:
            check_cancelled(ticket.cancellation)
            requested_monitor = self._requested_monitor
            # A failed second lookup must not revive the first target at speech time.
            self._shown = None
            self._requested_spatial = True
        self.dispatch(ticket)
        reference = requested_monitor or monitor_reference(query) or normalize_monitor_reference(monitor_id)
        try:
            monitor = self.monitors.resolve(reference)
        except ValueError as error:
            return {"status": "invalid_monitor", "message": str(error), **self.get_monitors()}
        if not query_words(query):
            return {"status": "ambiguous", "message": "Which icon or object do you mean? Give its name or describe its appearance; I won't guess."}
        result = self._find(query, monitor, ticket.cancellation)
        check_cancelled(ticket.cancellation)
        return result

    def _refresh_window(self, previous, monitor, cancellation):
        """Keep native identity, refresh visibility/bounds, never re-search its title.

        Discord/browser titles contain changing channel/page names, question stop
        words, and even words like 'button'. They are labels, not new user intent.
        """
        windows = self.grounder.windows(cancellation)
        check_cancelled(cancellation)
        window = next((item for item in windows if item.handle == previous.window_handle
                       and item.pid == previous.window_pid and not item.is_desktop), None)
        if window is not None:
            parts = [part.intersection(monitor.physical) for part in visible_parts(window.bounds, window, windows)]
            bounds = max(parts, key=lambda part: part.area, default=None)
            if bounds is not None and bounds.area >= 100:
                target = self._register(window.title, bounds, "window", previous.confidence, window,
                                        query=previous.query, monitor=monitor)
                if target is not None:
                    return target
        raise TargetUnavailable("The target moved, disappeared, or became covered. Locate it again.")

    def _fresh(self, target_id, cancellation):
        check_cancelled(cancellation)
        self._require_enabled()
        with self._lock:
            previous = self._targets.get(target_id)
        if previous is None:
            raise ValueError("Unknown target ID. Locate it first; arbitrary coordinates are not accepted.")
        if previous.revision != self.monitors.revision or time.monotonic() - previous.timestamp > SPATIAL_TARGET_TTL_SECONDS:
            raise TargetUnavailable("Target expired or monitor layout changed. Locate it again.")
        monitor = self.monitors.resolve(previous.monitor_id)
        if previous.source == "window":
            return self._refresh_window(previous, monitor, cancellation)
        if previous.source == "ocr":
            reader = self.reader()
            detections = reader.locate_text(previous.label, monitor.id, cancellation) if reader else []
            matches = [item for item in detections if item["bounds"].intersection(previous.physical_box).area >= previous.physical_box.area * 0.7]
            if len(matches) != 1:
                raise TargetUnavailable("The visual target moved or disappeared. Locate it again.")
            item = matches[0]
            return self._register(item["label"], item["bounds"], "ocr", item["confidence"], query=previous.query, monitor=monitor)
        result = self._find(previous.query or previous.label, monitor, cancellation, previous=previous, visual=False)
        if not result or result.get("status") != "found":
            raise TargetUnavailable("The target moved, disappeared, or became covered. Locate it again.")
        with self._lock:
            return self._targets[result["target"]["target_id"]]

    def _unavailable(self, ticket, message):
        check_cancelled(ticket.cancellation)
        with self._lock:
            if ticket.generation != self._generation:
                raise OperationCancelled()
            self._invalidated = self._invalidated or self._shown is not None
            self._shown = None
        self.dispatch(ActionTicket("cancel", ticket.generation, ticket.cancellation))
        return {"status": "unavailable", "message": message,
                "instruction": "No current pointing target was confirmed. Explain that it is unavailable; do not claim to be pointing at it."}

    def teleport_to_target(self, target_id, placement="auto", cancel_event=None, duration_ms=None):
        if placement != "auto":
            raise ValueError("Only validated automatic placement is supported")
        ticket = self._ticket("show", cancel_event, duration_ms=duration_ms)
        try:
            ticket.target = self._fresh(target_id, ticket.cancellation)
        except TargetUnavailable as error:
            return self._unavailable(ticket, str(error))
        check_cancelled(ticket.cancellation)
        self.dispatch(ticket)
        deadline = time.monotonic() + 5
        while not ticket.done.wait(0.025):
            check_cancelled(ticket.cancellation)
            if time.monotonic() > deadline:
                ticket.cancellation.events[0].set()
                raise ValueError("The GUI did not confirm the pointing action. No pointing claim is safe.")
        check_cancelled(ticket.cancellation)
        if ticket.result.get("status") != "pointing":
            return self._unavailable(ticket, ticket.result.get("message", "Pointing was cancelled"))
        with self._lock:
            if ticket.generation != self._generation:
                raise OperationCancelled()
            self._shown = ticket.target.id
            self._invalidated = False
            self._last_target = ticket.target.id
        return ticket.result

    def point_at_target(self, target_id, duration_ms=None, cancel_event=None):
        if duration_ms is not None and (type(duration_ms) is not int or not 200 <= duration_ms <= 30000):
            raise ValueError("duration_ms must be an integer from 200 to 30000")
        return self.teleport_to_target(target_id, cancel_event=cancel_event, duration_ms=duration_ms)

    def show_target(self, query, monitor_id=None, cancel_event=None):
        result = self.locate_on_screen(query, monitor_id, cancel_event)
        if result and result.get("status") == "found":
            return self.teleport_to_target(result["target"]["target_id"], cancel_event=cancel_event)
        return result

    def return_to_previous(self, cancel_event=None):
        ticket = self._ticket("return", cancel_event)
        self.dispatch(ticket)
        deadline = time.monotonic() + 5
        while not ticket.done.wait(0.025):
            check_cancelled(ticket.cancellation)
            if time.monotonic() > deadline:
                ticket.cancellation.events[0].set()
                raise ValueError("The GUI did not confirm the return action")
        check_cancelled(ticket.cancellation)
        if ticket.result.get("status") == "returned":
            with self._lock:
                if ticket.generation == self._generation:
                    self._shown = None
                    self._invalidated = False
        return ticket.result

    def prepare_response(self, cancel_event=None):
        """Acknowledge a refreshed pose before text/TTS can claim 'right there'."""
        with self._lock:
            shown = self._shown
            invalidated = self._invalidated
        unavailable = "That target moved or became unavailable before I could show it reliably. Ask me to locate it again, chum."
        if invalidated:
            return unavailable
        if shown is None:
            return None
        try:
            result = self.teleport_to_target(shown, cancel_event=cancel_event)
            if result.get("status") != "pointing":
                return unavailable
        except OperationCancelled:
            raise
        except ValueError:
            self.dispatch(self._ticket("cancel"))
            with self._lock:
                self._shown = None
                self._invalidated = True
            return unavailable
        return None

    def guard_response(self, answer):
        with self._lock:
            unshown = self._requested_spatial and self._shown is None
        if unshown and re.search(r"\b(right there|i(?:'m| am) pointing|i (?:have )?(?:teleported|moved)|look over here)\b", answer, re.I):
            return "I haven't verified a visible target to point at yet. Which app, control, or icon do you mean?"
        return answer

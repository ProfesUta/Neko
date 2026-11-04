"""Per-conversation bindings for spatial tools; no global GUI singleton."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

_session = ContextVar("capri_spatial_tools", default=(None, None))


@contextmanager
def spatial_session(service, cancel_event):
    token = _session.set((service, cancel_event))
    try:
        yield
    finally:
        _session.reset(token)


def _call(name, **arguments):
    service, cancellation = _session.get()
    if service is None:
        raise ValueError("Spatial awareness is unavailable in this session")
    if name != "get_monitors":
        arguments["cancel_event"] = cancellation
    return json.dumps(getattr(service, name)(**arguments), ensure_ascii=False)


def get_monitors() -> str:
    """List connected monitor IDs, numbers, physical/logical bounds, usable areas, and scaling."""
    return _call("get_monitors")


def get_visible_windows() -> str:
    """List actually visible, non-minimized windows with app names, bounds, and monitor IDs."""
    return _call("get_visible_windows")


def locate_on_screen(query: str, monitor_id: str = "") -> str:
    """Find a visible app/control by name. Returns a verified target_id or uncertainty; does not move."""
    return _call("locate_on_screen", query=query, monitor_id=monitor_id or None)


def teleport_to_target(target_id: str, placement: str = "auto") -> str:
    """Refresh a previously verified target_id, safely teleport beside it, and point. Never supply coordinates."""
    return _call("teleport_to_target", target_id=target_id, placement=placement)


def point_at_target(target_id: str, duration_ms: int = 5000) -> str:
    """Point toward a verified target_id, moving beside it if needed. Waits for the GUI to confirm."""
    return _call("point_at_target", target_id=target_id, duration_ms=duration_ms)


def show_target(query: str, monitor_id: str = "") -> str:
    """Locate a named visible object, teleport beside it, point, and highlight. Explain only after status=pointing. Ask for clarification on ambiguous/not_found/unverified results."""
    return _call("show_target", query=query, monitor_id=monitor_id or None)


def return_to_previous() -> str:
    """Return Capri to her position before the latest contextual jump, clamped to connected monitors."""
    return _call("return_to_previous")


SPATIAL_TOOLS = [get_monitors, get_visible_windows, locate_on_screen, teleport_to_target,
                 point_at_target, show_target, return_to_previous]
SPATIAL_NAMES = {function.__name__ for function in SPATIAL_TOOLS}

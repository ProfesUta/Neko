"""Bindings to existing League context and explicit offline analysis service."""
import json
from contextlib import contextmanager
from contextvars import ContextVar

_bound = ContextVar("capri_league_tools",default=(None,None,None))
_visual = ContextVar("capri_league_visual_tools",default=None)


@contextmanager
def league_session(league,replay,cancellation,visual=None):
    token = _bound.set((league,replay,cancellation))
    visual_token = _visual.set(visual)
    try:
        yield
    finally:
        _bound.reset(token)
        _visual.reset(visual_token)


def get_league_reference(query: str) -> str:
    """Retrieve a small patch-labelled cached champion/item reference. Never infer enemy positions or ability interactions."""
    league, _, cancellation = _bound.get()
    if league is None:
        return json.dumps({"status":"unavailable"})
    answer = league.companion_answer(query,cancellation)
    return json.dumps({"answer":answer or "No matching verified reference.","reference":league.knowledge.context_for(query)})


def analyze_league_recording(video_path: str, calibration_path: str) -> str:
    """Queue an explicit post-match local player-view recording. Use user-supplied video/calibration paths. Returns immediately; never analyzes live screens. Icons need human review."""
    _, replay, _ = _bound.get()
    return json.dumps(replay.submit(video_path,calibration_path) if replay is not None else {"status":"unavailable"})


def get_league_recording_status() -> str:
    """Read the existing offline job's status and report path without starting another job."""
    _, replay, _ = _bound.get()
    return json.dumps(replay.status() if replay is not None else {"status":"unavailable"})


def load_league_recording_report(report_path: str) -> str:
    """Load a player-view report after manual review. Never mark candidates confirmed automatically or infer missed sightings."""
    _, replay, _ = _bound.get()
    return json.dumps(replay.load_reviewed(report_path) if replay is not None else {"status":"unavailable"})


def open_league_vision_calibration(video_path: str = "") -> str:
    """Open local recorded-frame calibration and manual sighting-label editor. Use a supplied file path. Never enables live competitive capture or alerts."""
    service=_visual.get()
    return json.dumps(service.open_calibration(video_path) if service is not None else {"status":"unavailable"})


def get_league_vision_status() -> str:
    """Read the cached local CV status and approval gate without screen capture or inference."""
    service=_visual.get()
    return json.dumps(service.status() if service is not None else {"status":"unavailable"})


LEAGUE_TOOLS = [get_league_reference,analyze_league_recording,get_league_recording_status,load_league_recording_report,
                open_league_vision_calibration,get_league_vision_status]

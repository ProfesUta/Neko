"""Window titles describe content; native window identity owns refreshes."""

import json
from dataclasses import replace
from threading import Event

import pytest

from core.chat import _execute_tool
from core.context import Context
from core.spatial import TargetUnavailable
from core.spatial_geometry import Rect
from core.window_grounding import VisibleWindow
from tests.test_spatial import Grounder, gui_spatial, run_on_worker, service


@pytest.mark.parametrize("title", [
    "general - My Server - Discord",
    "the-lobby - Discord",
    "button-design - Discord",
    "icon-discussion - Discord",
])
def test_same_discord_window_refreshes_with_realistic_channel_titles(title):
    backend = Grounder([VisibleWindow(10, 20, title, "Discord.exe", Rect(100, 100, 700, 600))])
    spatial = service(grounder=backend)
    located = spatial.locate_on_screen("Discord")
    assert located["status"] == "found"
    refreshed = spatial._fresh(located["target"]["target_id"], Event())
    assert refreshed.window_handle == 10 and refreshed.window_pid == 20
    assert refreshed.label == title


def test_changing_title_and_bounds_of_same_window_remain_verified_without_using_vision():
    backend = Grounder([VisibleWindow(10, 20, "general - My Server - Discord", "Discord.exe", Rect(100, 100, 700, 600))])
    spatial = service(grounder=backend)
    target = spatial.locate_on_screen("Discord")["target"]["target_id"]
    backend.items = [replace(backend.items[0], title="(2) voice-chat - Another Server - Discord",
                             bounds=Rect(250, 150, 650, 550))]
    refreshed = spatial._fresh(target, Event())
    assert refreshed.window_bounds == backend.items[0].bounds
    assert refreshed.label == backend.items[0].title


def test_realistic_discord_title_completes_teleport_beam_and_response_refresh(gui_spatial, wait_until):
    window, spatial, backend = gui_spatial
    backend.items[0] = replace(backend.items[0], title="general - My Server - Discord")
    spatial.begin_turn("Where is Discord?")
    result = run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    assert result["status"] == "pointing"
    assert window.movement_coordinator.highlight.beam is not None
    backend.items[0] = replace(backend.items[0], title="(3) voice-chat - My Server - Discord")
    assert run_on_worker(spatial.prepare_response, wait_until) is None
    assert window.movement_coordinator.target.label == backend.items[0].title
    assert window.movement_coordinator.highlight.beam is not None


@pytest.mark.parametrize("replacement", [
    VisibleWindow(10, 21, "Discord", "Discord.exe", Rect(100, 100, 700, 600)),
    VisibleWindow(11, 20, "Discord", "Discord.exe", Rect(100, 100, 700, 600)),
])
def test_window_refresh_never_switches_to_different_window_or_reused_handle(replacement):
    backend = Grounder([VisibleWindow(10, 20, "Discord", "Discord.exe", Rect(100, 100, 700, 600))])
    spatial = service(grounder=backend)
    target = spatial.locate_on_screen("Discord")["target"]["target_id"]
    backend.items = [replacement]
    with pytest.raises(TargetUnavailable):
        spatial._fresh(target, Event())


def test_refresh_recomputes_visible_fragment_and_refuses_fully_covered_window():
    discord = VisibleWindow(10, 20, "general - My Server - Discord", "Discord.exe", Rect(100, 100, 700, 600), 1)
    backend = Grounder([discord])
    spatial = service(grounder=backend)
    target = spatial.locate_on_screen("Discord")["target"]["target_id"]
    cover = VisibleWindow(11, 21, "Browser", "chrome.exe", Rect(0, 0, 450, 900), 0)
    backend.items = [cover, discord]
    refreshed = spatial._fresh(target, Event())
    assert refreshed.physical_box == Rect(450, 100, 350, 600)
    backend.items[0] = replace(cover, bounds=Rect(0, 0, 1200, 900))
    with pytest.raises(TargetUnavailable):
        spatial._fresh(target, Event())


def test_genuine_target_loss_is_structured_tool_result_without_error_traceback(caplog):
    class Disappearing(Grounder):
        def windows(self, cancel_event=None):
            items = self.items
            self.items = []
            return items
    context = Context()
    tickets = []
    context.spatial = service(grounder=Disappearing([
        VisibleWindow(10, 20, "general - My Server - Discord", "Discord.exe", Rect(100, 100, 700, 600)),
    ]), dispatch=tickets.append)
    context.spatial.begin_turn("Where is Discord?")
    result = json.loads(_execute_tool("show_target", {"query": "Discord"}, context))
    assert result["status"] == "unavailable"
    assert not any(ticket.kind == "show" for ticket in tickets)
    assert tickets[-1].kind == "cancel"
    assert "Tool show_target failed" not in caplog.text
    assert "haven't verified" in context.spatial.guard_response("Right there!")


def test_control_refresh_preserves_original_query_and_requires_same_parent_process():
    browser = VisibleWindow(10, 20, "My browser", "chrome.exe", Rect(100, 100, 700, 600))
    button = {"name": "Download 2 files", "handle": "10", "runtime_id": "4.5.6",
              "control_type": "ControlType.Button", "x": 150, "y": 150, "width": 200, "height": 35}
    backend = Grounder([browser], [button])
    spatial = service(grounder=backend)
    target = spatial.locate_on_screen("Download button")["target"]["target_id"]
    backend.controls[0] = {**button, "name": "Download 3 files"}
    assert spatial._fresh(target, Event()).label == "Download 3 files"
    backend.items[0] = replace(browser, pid=21)
    with pytest.raises(TargetUnavailable):
        spatial._fresh(target, Event())
    backend.items[0] = browser
    backend.controls[0] = {**button, "name": "Delete 3 files"}
    with pytest.raises(TargetUnavailable):
        spatial._fresh(target, Event())

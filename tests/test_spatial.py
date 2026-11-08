import time
from dataclasses import replace
from threading import Event, Thread

import pytest
from PySide6.QtCore import QPoint

from core.cancellation import OperationCancelled
from core.spatial import ActionState, SpatialService, monitor_reference, spatial_request
from core.spatial_geometry import CaptureTransform, Monitor, Rect, SpriteMetrics, place_beside
from core.window_grounding import VisibleWindow, visible_parts


class Monitors:
    def __init__(self, monitors):
        self.monitors = tuple(monitors)
        self.revision = 1

    def snapshot(self):
        return self.monitors

    def resolve(self, reference=None):
        if reference is None:
            return None
        for monitor in self.monitors:
            if str(reference) in (monitor.id, str(monitor.number), monitor.name):
                return monitor
        raise ValueError("Monitor disconnected")


class Grounder:
    supported = True

    def __init__(self, windows=(), elements=()):
        self.items = list(windows)
        self.controls = list(elements)

    def windows(self, cancel_event=None):
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled()
        return list(self.items)

    def elements(self, windows, cancel_event=None, runtime_id=""):
        return list(self.controls)

    def unchanged(self, handle, pid, bounds):
        return any(window.handle == handle and window.pid == pid and window.bounds == bounds for window in self.items)


def layouts():
    return [
        Monitor("primary", 1, "Primary", Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 1, True),
        Monitor("left", 2, "Left", Rect(-2560, 0, 1707, 960), Rect(-2560, 0, 2560, 1440), Rect(-2560, 0, 1707, 920), 1.5),
        Monitor("above", 3, "Above", Rect(0, -2160, 1920, 1080), Rect(0, -2160, 3840, 2160), Rect(0, -2160, 1920, 1040), 2),
        Monitor("below", 4, "Below", Rect(0, 1080, 1280, 720), Rect(0, 1080, 1280, 720), Rect(0, 1080, 1280, 680)),
    ]


def service(monitors=None, grounder=None, reader=None, dispatch=None):
    return SpatialService(monitors or Monitors(layouts()), grounder or Grounder(),
                          lambda: reader, lambda: True, dispatch or (lambda ticket: None))


@pytest.mark.parametrize("monitor", layouts())
def test_capture_coordinates_round_trip_across_negative_vertical_and_scaled_displays(monitor):
    transform = CaptureTransform(monitor, 800, 450)
    image_box = Rect(100, 50, 200, 100)
    physical = transform.image_to_physical(image_box)
    logical = transform.image_to_logical(image_box)
    restored = monitor.logical_to_physical(logical)
    assert restored.x == pytest.approx(physical.x)
    assert restored.y == pytest.approx(physical.y)
    assert restored.width == pytest.approx(physical.width)
    assert restored.height == pytest.approx(physical.height)
    assert monitor.physical.contains(physical)
    assert monitor.logical.contains(logical)
    with pytest.raises(ValueError):
        transform.image_to_logical(Rect(-1, 0, 10, 10))


def test_scaling_is_local_to_each_monitor_not_applied_to_global_origin():
    monitor = layouts()[1]
    assert monitor.physical_to_logical(Rect(-2560, 0, 256, 144)).x == -2560
    assert monitor.physical_to_logical(Rect(-2304, 144, 256, 144)).x == pytest.approx(-2389.3)


def test_friendly_qscreen_names_match_native_device_ids_at_mixed_dpi():
    from core.monitors import match_native_monitor
    native = {"DISPLAY1": Rect(0, 0, 1920, 1080), "DISPLAY2": Rect(-2560, 0, 2560, 1440)}
    identity, physical = match_native_monitor("Same model name", Rect(-2560, 0, 1707, 960), 1.5, native)
    assert identity == "DISPLAY2"
    assert physical == native[identity]
    assert match_native_monitor("Unknown", Rect(5000, 0, 1000, 1000), 2, native) == (None, None)


@pytest.mark.parametrize("monitor", layouts())
@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_placement_keeps_actual_sprite_inside_display_without_covering_small_target(monitor, edge):
    bounds = monitor.available
    x = bounds.x + 8 if edge == "left" else bounds.right - 68 if edge == "right" else bounds.center[0]
    y = bounds.y + 8 if edge == "top" else bounds.bottom - 38 if edge == "bottom" else bounds.center[1]
    target = Rect(x, y, 60, 30)
    metrics = SpriteMetrics(Rect(25, 5, 260, 295), (40, 90), (270, 90))
    placement = place_beside(target, bounds, metrics)
    body = metrics.visible.translated(placement.x, placement.y)
    assert bounds.contains(body)
    assert body.intersection(target).area == 0
    hand = metrics.left_hand if placement.direction == "left" else metrics.right_hand
    assert (target.center[0] <= placement.x + hand[0]) == (placement.direction == "left")


def test_fullscreen_target_uses_best_safe_position_when_perfect_placement_is_impossible():
    bounds = layouts()[0].available
    metrics = SpriteMetrics(Rect(20, 10, 260, 290), (35, 90), (270, 90))
    placement = place_beside(bounds, bounds, metrics)
    assert bounds.contains(metrics.visible.translated(placement.x, placement.y))
    assert placement.overlap > 0


def test_native_window_is_located_on_correct_monitor_and_not_primary_by_default():
    backend = Grounder([VisibleWindow(11, 50, "Discord", "Discord.exe", Rect(-2300, 200, 600, 700))])
    spatial = service(grounder=backend)
    result = spatial.locate_on_screen("Where's Discord?")
    assert result["status"] == "found"
    target = result["target"]
    assert target["monitor_id"] == "left"
    assert target["source"] == "window"
    assert target["bounding_box"]["x"] < 0
    assert spatial.locate_on_screen("Discord", "1")["status"] == "not_found"
    assert spatial.get_visible_windows()["windows"][0]["monitor_ids"] == ["left"]


def test_occluded_window_uses_visible_fragment_and_completely_covered_window_is_not_found():
    cover = VisibleWindow(1, 51, "Browser", "chrome.exe", Rect(0, 0, 600, 800), 0)
    app = VisibleWindow(2, 52, "Discord", "Discord.exe", Rect(400, 100, 600, 600), 1)
    spatial = service(grounder=Grounder([cover, app]))
    result = spatial.locate_on_screen("Discord")
    assert result["target"]["bounding_box"]["x"] == 600
    spatial.grounder.items[0] = replace(cover, bounds=Rect(0, 0, 1600, 1000))
    assert spatial.locate_on_screen("Discord")["status"] == "not_found"
    assert visible_parts(app.bounds, app, spatial.grounder.items) == []


def test_accessible_button_uses_real_bounds_and_rejects_covered_control():
    window = VisibleWindow(5, 20, "Browser", "chrome.exe", Rect(0, 0, 1200, 900), 1)
    button = {"name": "Download", "handle": "5", "runtime_id": "4.5.6", "control_type": "ControlType.Button",
              "x": 800, "y": 300, "width": 100, "height": 30}
    backend = Grounder([window], [button])
    spatial = service(grounder=backend)
    result = spatial.locate_on_screen("Where's the download button?")
    assert result["target"]["source"] == "accessibility"
    assert result["target"]["physical_box"] == Rect(800, 300, 100, 30).to_dict()
    backend.items.insert(0, VisibleWindow(6, 21, "Cover", "other.exe", Rect(750, 250, 300, 100), 0))
    assert spatial.locate_on_screen("download button")["status"] == "not_found"


def test_named_desktop_icon_can_be_grounded_without_treating_wallpaper_as_an_app():
    desktop = VisibleWindow(7, 30, "Desktop", "explorer.exe", Rect(-2560, 0, 4480, 1440), 10, True)
    icon = {"name": "Recycle Bin", "handle": "7", "runtime_id": "5.4.3", "control_type": "ControlType.ListItem",
            "x": -2500, "y": 100, "width": 90, "height": 90}
    spatial = service(grounder=Grounder([desktop], [icon]))
    result = spatial.locate_on_screen("Recycle Bin icon", "2")
    assert result["target"]["source"] == "accessibility"
    assert result["target"]["monitor_id"] == "left"
    assert result["target"]["physical_box"]["x"] == -2500
    assert spatial.locate_on_screen("Explorer")["status"] == "not_found"


def test_ambiguous_windows_and_unspecified_icons_never_create_action_targets():
    backend = Grounder([VisibleWindow(1, 10, "Page one", "chrome.exe", Rect(0, 0, 500, 500), 0),
                        VisibleWindow(2, 10, "Page two", "chrome.exe", Rect(600, 0, 500, 500), 1)])
    spatial = service(grounder=backend)
    assert spatial.locate_on_screen("my browser")["status"] == "ambiguous"
    assert spatial.locate_on_screen("What's that icon on my second monitor?")["status"] == "ambiguous"
    assert not spatial._targets


def test_app_name_in_an_unrelated_browser_page_is_not_mistaken_for_the_app():
    backend = Grounder([VisibleWindow(10, 20, "Discord installation instructions - Chrome", "chrome.exe", Rect(0, 0, 900, 700))])
    spatial = service(grounder=backend)
    assert spatial.locate_on_screen("Where's Discord?")["status"] == "not_found"
    assert not spatial._targets


def test_model_coordinate_payloads_and_unverified_visual_boxes_cannot_move_capri():
    class Reader:
        def locate_visual(self, *args):
            return {"label": "", "monitor_id": "left", "bounding_box": [0, 0, 9999, 9999]}, []
    spatial = service(reader=Reader())
    assert spatial.show_target("red icon")["status"] == "unverified"
    with pytest.raises(ValueError, match="Unknown target"):
        spatial.teleport_to_target('{"x": 300, "y": 50}')
    assert not spatial._targets


def test_window_refresh_follows_actual_movement_and_rejects_disappearance_and_topology_changes():
    window = VisibleWindow(1, 20, "Discord", "Discord.exe", Rect(100, 100, 700, 600))
    backend = Grounder([window])
    spatial = service(grounder=backend)
    target = spatial.locate_on_screen("Discord")["target"]["target_id"]
    backend.items = [replace(window, bounds=Rect(-2200, 100, 700, 600))]
    # A target is tied to a display until a fresh locate confirms a cross-display move.
    with pytest.raises(ValueError):
        spatial._fresh(target, Event())
    moved = spatial.locate_on_screen("Discord")["target"]["target_id"]
    backend.items = []
    with pytest.raises(ValueError, match="moved, disappeared"):
        spatial._fresh(moved, Event())
    spatial.monitors.revision += 1
    with pytest.raises(ValueError, match="layout changed"):
        spatial._fresh(moved, Event())


def test_old_action_tickets_are_cancelled_by_new_request_and_old_finish_cannot_end_new_turn():
    tickets = []
    spatial = service(dispatch=tickets.append)
    old = spatial.begin_turn("Where's Discord?")
    old_ticket = spatial._ticket("show")
    new = spatial.begin_turn("Where's my browser?")
    assert new > old
    assert old_ticket.cancellation.is_set()
    count = len(tickets)
    spatial.finish_turn(old)
    assert len(tickets) == count
    spatial.cancel()
    assert spatial._ticket("show").cancellation.is_set()


def test_monitor_reference_and_action_claim_guard():
    assert monitor_reference("on my second monitor") == "2"
    assert monitor_reference("display 4") == "4"
    assert spatial_request("Where’s Discord?")
    assert spatial_request("What's that icon on my second monitor?")
    spatial = service()
    spatial.begin_turn("Where's Discord?")
    assert "haven't verified" in spatial.guard_response("Right there, chum!")


@pytest.fixture
def gui_spatial(qapp):
    from core.context import Context
    from gui.window import CapriWindow
    window = CapriWindow([{"role": "system", "content": "persona"}], Context())
    window.show()
    window._movement_timer.stop()
    qapp.processEvents()
    window.teleport_to(20, 20, effects=False)
    backend = Grounder([VisibleWindow(10, 20, "Discord", "Discord.exe", Rect(500, 180, 170, 170))])
    window.context.spatial.grounder = backend
    window.context.spatial.enabled = lambda: True
    window.movement_coordinator.grounder = backend
    yield window, window.context.spatial, backend
    window.close()


def run_on_worker(function, wait_until):
    done, result = Event(), {}
    def run():
        try:
            result["value"] = function()
        except Exception as error:
            result["error"] = error
        finally:
            done.set()
    thread = Thread(target=run, daemon=True)
    thread.start()
    wait_until(done.is_set)
    thread.join(0.1)
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_gui_arrival_pointing_highlight_and_acknowledgement_precede_response(gui_spatial, wait_until, monkeypatch):
    import config
    window, spatial, backend = gui_spatial
    monkeypatch.setattr(config, "TARGET_HIGHLIGHT_DURATION_MS", 200)
    bursts = []
    play = window.teleport_effects.play
    def record_play(*args):
        play(*args)
        bursts.extend(window.teleport_effects.bursts)
    monkeypatch.setattr(window.teleport_effects, "play", record_play)
    generation = spatial.begin_turn("Where's Discord?")
    result = run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    assert result["status"] == "pointing"
    assert window.sprite.pointing_direction == result["direction"]
    assert window.movement_coordinator.state == ActionState.POINTING
    highlight = window.movement_coordinator.highlight
    assert highlight._overlay.isVisible()
    from PySide6.QtCore import Qt
    assert highlight._overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert len(bursts) == 2
    position = QPoint(window.pos())
    window._next_wander_at = 0
    window._move_pet()
    assert window.pos() == position
    window.set_status("Speaking")
    assert window.sprite.pointing_direction is not None
    assert window.movement_coordinator.state == ActionState.SPEAKING
    wait_until(lambda: highlight.now_ms() > 220)
    # The brief outline can fade; the channel stays attached during speech.
    assert highlight._overlay.isVisible() and highlight.beam is not None
    spatial.finish_turn(generation)
    window.movement_coordinator.reset()
    window.set_status("Listening")
    assert window.sprite.pointing_direction is None
    assert window.sprite._frames == ("listening",)
    assert not highlight._overlay.isVisible() and highlight.beam is None


def test_window_change_after_detection_before_gui_ack_prevents_teleport(gui_spatial, wait_until):
    window, spatial, backend = gui_spatial
    spatial.begin_turn("Where's Discord?")
    original = window.pos()
    real_dispatch = spatial.dispatch
    def dispatch(ticket):
        if ticket.kind == "show":
            backend.items = []
        real_dispatch(ticket)
    spatial.dispatch = dispatch
    result = run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    assert result["status"] == "unavailable"
    assert "moved or disappeared" in result["message"]
    assert window.pos() == original
    assert not window.sprite.pointing_direction
    assert not window.teleport_effects.bursts


def test_monitor_disconnection_cancels_pointing_and_recovers_safe_position(gui_spatial, wait_until):
    window, spatial, backend = gui_spatial
    spatial.begin_turn("Where's Discord?")
    run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    window.monitor_manager.revision += 1
    window._on_monitors_changed()
    assert not window.movement_coordinator.busy
    assert window.sprite.pointing_direction is None
    assert not window.movement_coordinator.highlight._overlay.isVisible()
    assert window._clamp_position(window.x(), window.y()) == (window.x(), window.y())


def test_pointing_mirroring_preserves_stable_feet_and_silhouette_cache(gui_spatial):
    window, spatial, backend = gui_spatial
    sprite = window.sprite
    feet = []
    for direction in ("left", "right"):
        assert sprite.start_pointing(direction)
        shape = sprite.teleport_silhouette()
        assert shape is sprite.teleport_silhouette()
        feet.append(sprite._pointing_transform(direction).map(sprite._feet(sprite._silhouettes["pointingleft"])))
        sprite.set_activity("speaking")
        assert sprite.pointing_direction == direction
    assert feet[0] == feet[1]
    sprite.end_pointing()
    assert sprite._frames == ("talking",)


def test_real_tool_loop_waits_for_pointing_before_tts_and_skips_generic_screen_read(gui_spatial, wait_until, monkeypatch):
    import config
    import ollama
    import voice.tts as tts
    from core import chat as engine
    window, spatial, backend = gui_spatial
    monkeypatch.setattr(config, "SPATIAL_QUICK_ACTIONS_ENABLED", False)
    monkeypatch.setattr(config, "SPATIAL_POINTING_HOLD_MS", 0)
    class Reader:
        def describe_question(self, *args):
            raise AssertionError("Spatial requests must use the verified tool path")
    window.context.set_screen_reader(Reader())
    replies = iter([
        ollama.ChatResponse(message={"role": "assistant", "tool_calls": [
            {"function": {"name": "show_target", "arguments": {"query": "Discord"}}}]}),
        ollama.ChatResponse(message={"role": "assistant", "content": "Right there on monitor 1, chum!"}),
    ])
    monkeypatch.setattr(engine, "request_chat", lambda **kwargs: next(replies))
    played = []
    def speak(text, **kwargs):
        assert kwargs["before_playback"]() is None
        assert window.sprite.pointing_direction is not None
        assert spatial._shown is not None
        kwargs["on_playback_start"]()
        played.append(text)
    monkeypatch.setattr(tts, "speak", speak)
    window.send_text("Where's Discord?", speak_reply=True)
    wait_until(lambda: window.chat_worker is None)
    assert played == ["Right there on monitor 1, chum!"]
    assert window.sprite.pointing_direction is None
    assert "Right there" in window.chat_panel.transcript.toPlainText()


def test_new_location_request_cancels_old_inference_and_only_new_target_moves(gui_spatial, wait_until, monkeypatch):
    import config
    import ollama
    from core import chat as engine
    from core.cancellation import check_cancelled
    window, spatial, backend = gui_spatial
    monkeypatch.setattr(config, "SPATIAL_QUICK_ACTIONS_ENABLED", False)
    backend.items.append(VisibleWindow(30, 40, "Browser", "chrome.exe", Rect(80, 100, 300, 300), 1))
    monkeypatch.setattr(config, "SPATIAL_POINTING_HOLD_MS", 0)
    old_started = Event()
    browser_calls = []
    def request(cancel_event=None, **kwargs):
        latest = next(message["content"] for message in reversed(kwargs["messages"]) if message["role"] == "user")
        if "Discord" in latest:
            old_started.set()
            while True:
                check_cancelled(cancel_event)
                time.sleep(0.005)
        browser_calls.append(True)
        if len(browser_calls) == 1:
            return ollama.ChatResponse(message={"role": "assistant", "tool_calls": [
                {"function": {"name": "show_target", "arguments": {"query": "browser"}}}]})
        return ollama.ChatResponse(message={"role": "assistant", "content": "Here's your browser."})
    monkeypatch.setattr(engine, "request_chat", request)
    window.send_text("Where's Discord?")
    wait_until(old_started.is_set)
    window.send_text("Where's my browser?")
    wait_until(lambda: window.chat_worker is None and window._pending_action is None)
    assert len(browser_calls) == 2
    assert spatial._targets[spatial._last_target].label == "Browser"
    assert len(window.teleport_effects.bursts) == 2
    assert not window.movement_coordinator.busy


def test_control_coordinates_refresh_before_explanation_and_disappearance_replaces_claim(gui_spatial, wait_until):
    window, spatial, backend = gui_spatial
    backend.controls = [{"name": "Download", "handle": "10", "runtime_id": "1.2.3", "control_type": "ControlType.Button",
                         "x": 520, "y": 220, "width": 80, "height": 25}]
    spatial.begin_turn("Where's the download button?")
    run_on_worker(lambda: spatial.show_target("download button"), wait_until)
    backend.controls[0]["x"] = 570
    assert run_on_worker(lambda: spatial.prepare_response(), wait_until) is None
    assert window.movement_coordinator.target.physical_box.x == 570
    backend.controls = []
    replacement = run_on_worker(lambda: spatial.prepare_response(), wait_until)
    assert "moved or became unavailable" in replacement
    wait_until(lambda: not window.movement_coordinator.busy)
    assert window.sprite.pointing_direction is None


def test_return_does_not_jump_back_to_target_when_its_response_is_prepared(gui_spatial, wait_until):
    window, spatial, backend = gui_spatial
    original = QPoint(window.pos())
    spatial.begin_turn("Where's Discord?")
    run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    assert window.pos() != original
    result = run_on_worker(lambda: spatial.return_to_previous(), wait_until)
    assert result["status"] == "returned"
    assert window.pos() == original
    assert run_on_worker(lambda: spatial.prepare_response(), wait_until) is None
    assert window.pos() == original
    assert not window.sprite.pointing_direction


def test_topology_cancellation_after_synthesis_cannot_play_an_obsolete_pointing_claim(gui_spatial, wait_until, monkeypatch):
    from gui.workers import ChatWorker
    import voice.tts as tts
    window, spatial, backend = gui_spatial
    generation = spatial.begin_turn("Where's Discord?")
    run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    worker = ChatWorker(window.messages, window.context, "Where's Discord?", True)
    worker._spatial_generation = generation
    spoken = []
    monkeypatch.setattr(window.context.interaction, "record_spoken", spoken.append)
    def speak(text, **kwargs):
        spatial.cancel()  # A display disconnect/drag while the WAV is being synthesized.
        replacement = kwargs["before_playback"]()
        assert "moved or became unavailable" in replacement
        kwargs["on_playback_start"]()
    monkeypatch.setattr(tts, "speak", speak)
    run_on_worker(lambda: worker._speak("Right there, chum!"), wait_until)
    assert len(spoken) == 1
    assert "moved or became unavailable" in spoken[0]


def test_closed_window_does_not_wait_for_garbage_collection_on_an_arbitrary_thread(qapp):
    import gc
    import weakref
    from core.context import Context
    from gui.window import CapriWindow
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        window = CapriWindow([{"role": "system", "content": "persona"}], Context())
        reference = weakref.ref(window)
        window.show()
        qapp.processEvents()
        window.close()
        assert not window.sprite._timer.isActive()
        del window
        # No Python cycle may leave a QWidget to be destroyed by worker-thread GC.
        assert reference() is None
    finally:
        if was_enabled:
            gc.enable()

import json
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from core.chat import _execute_tool, build_request_messages
from core.context import Context
from core.monitors import MonitorManager
from core.spatial_geometry import Rect
from core.window_grounding import VisibleWindow
from gui.target_lightning import TargetLightning
from tests.test_spatial import Grounder, gui_spatial, run_on_worker, service


@pytest.mark.parametrize("reference", [None, "", "0", 0, " auto ", "all"])
def test_auto_monitor_search_finds_secondary_instead_of_defaulting_to_primary(reference):
    grounder = Grounder([VisibleWindow(1, 2, "Discord", "Discord.exe", Rect(-2400, 150, 450, 600))])
    spatial = service(grounder=grounder)
    result = spatial.locate_on_screen("Discord", reference)
    assert result["status"] == "found"
    assert result["target"]["monitor_id"] == "left"


def test_monitor_manager_accepts_sentinels_but_rejects_disconnected_ids(qapp):
    manager = MonitorManager()
    try:
        for value in ("0", 0, " all ", "auto", None):
            assert manager.resolve(value) is None
        assert manager.resolve("primary").primary
        with pytest.raises(ValueError):
            manager.resolve("disconnected")
    finally:
        manager.dispose()


def test_explicit_user_monitor_survives_zero_default_in_tool_arguments():
    grounder = Grounder([
        VisibleWindow(1, 2, "Discord", "Discord.exe", Rect(200, 150, 450, 600)),
        VisibleWindow(3, 4, "Discord", "Discord.exe", Rect(-2400, 150, 450, 600)),
    ])
    spatial = service(grounder=grounder)
    spatial.begin_turn("Where is Discord on my second monitor?")
    result = spatial.locate_on_screen("Discord", "0")
    assert result["target"]["monitor_id"] == "left"
    spatial.begin_turn("Where is Discord?")
    assert spatial.locate_on_screen("Discord", "0")["status"] == "ambiguous"


def test_invalid_tool_monitor_is_recoverable_data_and_model_gets_current_inventory():
    context = Context()
    context.spatial = service()
    result = json.loads(_execute_tool("show_target", {"query": "Discord", "monitor_id": "99"}, context))
    assert result["status"] == "invalid_monitor"
    assert len(result["monitors"]) == 4
    messages = build_request_messages([], context)
    assert '"connected_displays"' in messages[0]["content"]
    assert "Monitor numbers start at 1" in messages[0]["content"]


@pytest.mark.parametrize("reference", [0, None, "0"])
def test_tool_optional_monitor_numbers_are_normalized(reference):
    context = Context()
    context.spatial = service(grounder=Grounder([
        VisibleWindow(1, 2, "Discord", "Discord.exe", Rect(-2400, 150, 450, 600)),
    ]))
    result = json.loads(_execute_tool("locate_on_screen", {"query": "Discord", "monitor_id": reference}, context))
    assert result["status"] == "found" and result["target"]["monitor_id"] == "left"


@pytest.mark.parametrize("direction,x", [("left", 30), ("right", 570)])
def test_channel_starts_at_rendered_hand_and_ends_inside_verified_target(gui_spatial, wait_until, direction, x):
    window, spatial, backend = gui_spatial
    backend.items = [VisibleWindow(10, 20, "Discord", "Discord.exe", Rect(x, 180, 140, 110))]
    spatial.begin_turn("Where is Discord?")
    result = run_on_worker(lambda: spatial.show_target("Discord", "0"), wait_until)
    assert result["status"] == "pointing"
    assert window.sprite.pointing_direction == direction
    indicator = window.movement_coordinator.highlight
    assert indicator.beam.start == window.sprite.pointing_hand_global(window)
    assert indicator.beam.end == QPointF(*window.movement_coordinator.target.bounding_box.center)
    for path, _branches, _sparks in indicator.beam.variants:
        assert path.pointAtPercent(0) == indicator.beam.start
        assert path.pointAtPercent(1) == indicator.beam.end
    assert indicator._overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert indicator._overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    spatial.cancel()
    wait_until(lambda: not indicator._overlay.isVisible())
    assert indicator.beam is None and not indicator._timer.isActive()
    assert window.sprite.pointing_direction is None


def test_channel_can_be_disabled_without_disabling_short_lived_outline(gui_spatial, wait_until, monkeypatch):
    import config
    monkeypatch.setattr(config, "TARGET_LIGHTNING_ENABLED", False)
    monkeypatch.setattr(config, "TARGET_HIGHLIGHT_DURATION_MS", 200)
    window, spatial, _backend = gui_spatial
    spatial.begin_turn("Where's Discord?")
    run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    indicator = window.movement_coordinator.highlight
    assert indicator.beam is None and indicator._overlay.isVisible()
    wait_until(lambda: not indicator._overlay.isVisible())


def test_channel_can_show_without_outline_and_chat_alone_does_not_point(gui_spatial, wait_until, monkeypatch):
    import config
    monkeypatch.setattr(config, "TARGET_HIGHLIGHT_ENABLED", False)
    window, spatial, _backend = gui_spatial
    window.show_chat()
    assert window.sprite.pointing_direction is None
    assert window.sprite.expression != "pointingleft"
    spatial.begin_turn("Where's Discord?")
    run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    indicator = window.movement_coordinator.highlight
    assert indicator.beam is not None and indicator._overlay.isVisible()


@pytest.mark.parametrize("query,monitor_id", [("Nonexistent app", None), ("Discord", "99"), ("that icon", None)])
def test_failed_second_lookup_clears_old_beam_and_cannot_revive_old_target(gui_spatial, wait_until, query, monitor_id):
    window, spatial, _backend = gui_spatial
    spatial.begin_turn("Where's Discord?")
    run_on_worker(lambda: spatial.show_target("Discord"), wait_until)
    result = run_on_worker(lambda: spatial.show_target(query, monitor_id), wait_until)
    assert result["status"] in ("not_found", "invalid_monitor", "ambiguous")
    assert window.sprite.pointing_direction is None
    assert window.movement_coordinator.highlight.beam is None
    assert spatial._shown is None
    assert "right there" not in spatial.guard_response("Right there!").lower()


@pytest.mark.parametrize("dpr", [1, 1.5, 2])
def test_lightning_negative_global_coordinates_render_at_same_logical_endpoint(dpr):
    start, end = QPointF(-2300, -200), QPointF(-2000, -100)
    beam = TargetLightning(start, end)
    viewport = QRectF(-2340, -260, 400, 240)
    image = QImage(round(viewport.width() * dpr), round(viewport.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.translate(-viewport.x(), -viewport.y())
    beam.paint(painter, 150)
    painter.end()
    x, y = round((end.x() - viewport.x()) * dpr), round((end.y() - viewport.y()) * dpr)
    assert image.pixelColor(x, y).alpha() > 0
    for path, _branches, _sparks in beam.variants:
        assert path.pointAtPercent(0) == start
        assert path.pointAtPercent(1) == end
        assert beam.bounds.contains(path.boundingRect())

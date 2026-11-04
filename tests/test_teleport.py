from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from core.context import Context
from gui.sprites import CharacterSprite
from gui.teleport import LightningBurst, MAX_ACTIVE_BURSTS, TeleportSettings, clamp_to_displays
from gui.window import CapriWindow


@pytest.fixture
def window(qapp):
    widget = CapriWindow([{"role": "system", "content": "persona"}], Context())
    widget.show()
    widget._movement_timer.stop()
    qapp.processEvents()
    widget.teleport_to(20, 20, effects=False)
    yield widget
    widget.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def destination(window):
    return QPoint(*window._clamp_position(430, 380))


def test_position_changes_before_vfx_and_character_never_hides(window, monkeypatch):
    previous = window.pos()
    observed = []
    real_play = window.teleport_effects.play
    def play(departure, arrival, silhouette):
        observed.append((window.pos(), window.sprite.isVisible(), window.windowOpacity()))
        real_play(departure, arrival, silhouette)
    monkeypatch.setattr(window.teleport_effects, "play", play)
    target = destination(window)
    assert window.teleport_to(target)
    assert window.pos() == target
    assert previous != target
    assert observed == [(target, True, 1.0)]
    bursts = window.teleport_effects.bursts
    assert len(bursts) == 2
    assert [burst.arrival for burst in bursts] == [False, True]
    assert bursts[0].started_ms == bursts[1].started_ms
    assert bursts[0].duration_ms == bursts[1].duration_ms == 200
    assert bursts[1].sprite_rect.topLeft() - bursts[0].sprite_rect.topLeft() == QPointF(target - previous)


def test_autonomous_idle_jumps_to_target_in_one_tick(window, monkeypatch):
    target = destination(window)
    monkeypatch.setattr(window, "_choose_movement_target", lambda radius=None: setattr(window, "_movement_target", target))
    window._next_wander_at = 0
    window._move_pet()
    assert window.pos() == target
    assert len(window.teleport_effects.bursts) == 2


def test_processing_teleports_are_paced_instead_of_firing_every_timer_tick(window, monkeypatch):
    now = [10.0]
    monkeypatch.setattr("gui.window.time.monotonic", lambda: now[0])
    def choose(radius=None):
        window._movement_target = QPoint(*window._clamp_position(window.x() + 40, window.y() + 25))
    monkeypatch.setattr(window, "_choose_movement_target", choose)
    window._set_movement_state("processing")
    window._move_pet()
    first = window.pos()
    for _ in range(20):
        window._move_pet()
    assert window.pos() == first
    assert len(window.teleport_effects.bursts) == 2
    now[0] += 0.46
    window._move_pet()
    assert window.pos() != first
    assert len(window.teleport_effects.bursts) == 4


def test_existing_programmatic_move_uses_effects_but_dragging_does_not(window):
    window.move(destination(window))
    assert len(window.teleport_effects.bursts) == 2
    window.teleport_effects.stop()
    window._drag_offset = QPoint(10, 10)
    window.move(30, 30)
    assert window.pos() == QPoint(30, 30)
    assert not window.teleport_effects.bursts
    window._move_pet()
    assert not window.teleport_effects.bursts
    window._drag_offset = None


def test_disabling_vfx_still_moves_instantly(window):
    window.teleport_effects.settings = replace(window.teleport_effects.settings, vfx_enabled=False)
    target = destination(window)
    window.teleport_to(target)
    assert window.pos() == target
    assert not window.teleport_effects.bursts
    assert not window.teleport_effects.active_windows


def test_same_position_and_hidden_startup_never_spawn_effects(window):
    assert not window.teleport_to(window.pos())
    assert not window.teleport_effects.bursts
    window.hide()
    window.move(40, 40)
    assert not window.teleport_effects.bursts


def test_effect_windows_are_transparent_clickthrough_nonactivating_and_screen_clipped(window):
    window.teleport_to(destination(window))
    assert window.teleport_effects.active_windows
    for overlay in window.teleport_effects.active_windows:
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert overlay.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert overlay.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
        assert overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert overlay.screen().geometry().contains(overlay.viewport)
        assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_cleanup_and_overlay_reuse_after_expiry(window, wait_until):
    manager = window.teleport_effects
    manager.settings = replace(manager.settings, duration_ms=50)
    original_windows = tuple(manager._overlays.values())
    window.teleport_to(destination(window))
    wait_until(lambda: not manager.bursts)
    assert not manager.active_windows
    assert not manager._timer.isActive()
    window.teleport_to(20, 20)
    assert tuple(manager._overlays.values()) == original_windows
    window.close()
    assert not manager._overlays
    assert not manager.bursts
    assert not manager._timer.isActive()
    assert not window._movement_timer.isActive()


def test_rapid_teleports_keep_bounded_bursts_and_windows(window):
    manager = window.teleport_effects
    pool = tuple(manager._overlays.values())
    for index in range(24):
        window.teleport_to(destination(window) if index % 2 == 0 else QPoint(20, 20))
        assert len(manager.bursts) <= MAX_ACTIVE_BURSTS
        assert tuple(manager._overlays.values()) == pool
    manager.stop()
    assert not manager.active_windows


def test_old_departure_lightning_cannot_cover_current_character_even_after_dragging(window):
    manager = window.teleport_effects
    shape = window.sprite.teleport_silhouette()
    current = window._sprite_screen_rect()
    departure = LightningBurst(current.translated(-20, 0), shape, manager.settings, 0, False, seed=4)
    manager.bursts = [departure]
    for offset in (0, 12):
        # Move during the still-active burst without spawning another one.
        window._drag_offset = QPoint(1, 1)
        window.move(20 + offset, 20)
        window._drag_offset = None
        current = window._sprite_screen_rect()
        body = LightningBurst(current, shape, manager.settings, 0, True, seed=5).body
        samples = [QPoint(x, y) for y in range(0, 400, 2) for x in range(0, 420, 2)
                   if body.contains(QRectF(x - 1, y - 1, 2, 2))]
        image = QImage(500, 450, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillPath(body, QColor("#35FF45"))
        baseline = image.copy()
        departure.paint(painter, 0)
        painter.end()
        # This short jump really would let the old lightning cross her body.
        assert any(image.pixelColor(point) != baseline.pixelColor(point) for point in samples)
        protected = baseline.copy()
        painter = QPainter(protected)
        manager.paint(painter, QRectF(0, 0, 500, 450), now_ms=0)
        painter.end()
        assert all(protected.pixelColor(point) == baseline.pixelColor(point) for point in samples)


def test_display_clamping_handles_negative_coordinates_edges_and_dpi_gaps():
    size = QSize(330, 350)
    bounds = [QRect(-1600, 0, 1600, 900), QRect(0, 0, 1536, 864), QRect(1920, 0, 2560, 1440)]
    assert clamp_to_displays(QPoint(-1200, 100), size, bounds) == QPoint(-1200, 100)
    assert clamp_to_displays(QPoint(-1900, -20), size, bounds) == QPoint(-1600, 0)
    assert clamp_to_displays(QPoint(2100, 100), size, bounds) == QPoint(2100, 100)
    assert clamp_to_displays(QPoint(1700, 100), size, bounds) == QPoint(1920, 100)
    assert clamp_to_displays(QPoint(5000, 2000), size, bounds) == QPoint(4150, 1090)


def test_silhouette_cache_tracks_body_and_hair_instead_of_image_rectangle(qapp):
    sprite = CharacterSprite()
    try:
        sprite.setFixedSize(300, 300)
        shape = sprite.teleport_silhouette()
        assert shape is sprite.teleport_silhouette()
        assert len(shape.anchors) >= 40
        assert not shape.path.contains(QPointF(0.01, 0.99))
        xs = [point.x() for point, normal in shape.anchors]
        ys = [point.y() for point, normal in shape.anchors]
        assert max(xs) - min(xs) > 0.5
        assert max(ys) - min(ys) > 0.85
    finally:
        sprite.close()


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_first_frame_has_lightning_outside_a_fully_visible_body(qapp, dpr):
    sprite = CharacterSprite()
    try:
        sprite.setFixedSize(300, 300)
        burst = LightningBurst(QRectF(100, 100, 300, 300), sprite.teleport_silhouette(), TeleportSettings(), 0, True, seed=4)
        size = round(560 * dpr)
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillPath(burst.body, QColor("#35FF45"))
        # Find a body-interior sample before painting the effect.
        sample = next(QPointF(x, y) for y in range(120, 390, 3) for x in range(110, 390, 3)
                      if burst.body.contains(QRectF(x - 2, y - 2, 4, 4)))
        before = image.pixelColor(round(sample.x() * dpr), round(sample.y() * dpr))
        burst.paint(painter, 0)
        painter.end()
        assert image.pixelColor(round(sample.x() * dpr), round(sample.y() * dpr)) == before
        red_pixels = sum(
            1 for y in range(0, size, 2) for x in range(0, size, 2)
            if image.pixelColor(x, y).red() > 180 and image.pixelColor(x, y).green() < 90
        )
        assert red_pixels > 100
        empty = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        empty.fill(Qt.GlobalColor.transparent)
        p = QPainter(empty)
        burst.paint(p, 201)
        p.end()
        assert all(empty.pixelColor(x, y).alpha() == 0 for x in range(0, size, 10) for y in range(0, size, 10))
    finally:
        sprite.close()


def test_new_happy_and_pointing_poses_restore_and_never_override_voice_state(qapp, wait_until):
    sprite = CharacterSprite()
    try:
        assert "happy" in sprite.sprite_names
        assert "pointingleft" in sprite.sprite_names
        assert sprite.flash_expression("pointingLeft", 20)
        assert sprite.state == "idle"
        assert sprite.expression == "pointingleft"
        wait_until(lambda: sprite.expression is None)
        assert sprite._frames == ("idle",)
        assert sprite.flash_expression("happy", 100)
        sprite.set_activity("listening")
        assert sprite.expression is None
        assert sprite._frames == ("listening",)
        assert not sprite.flash_expression("happy")
    finally:
        sprite.close()

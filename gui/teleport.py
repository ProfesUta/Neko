"""Procedural crimson/black teleport bursts on reusable click-through Qt overlays."""

import ctypes
import math
import os
import random
import sys
import weakref
from dataclasses import dataclass

from PySide6.QtCore import QElapsedTimer, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QGuiApplication, QPainter, QPainterPath, QPainterPathStroker,
    QPen, QPolygonF, QTransform,
)
from PySide6.QtWidgets import QWidget

import config

CRIMSON = "#FF0033"
BLACK = "#090009"
PURPLE = "#51002E"
NEON = "#FF4564"
MAX_ACTIVE_BURSTS = 12
FRAME_INTERVAL_MS = 16


@dataclass(frozen=True)
class TeleportSettings:
    enabled: bool = True
    vfx_enabled: bool = True
    duration_ms: int = 200
    intensity: float = 1.0
    spark_count: int = 12

    @classmethod
    def from_config(cls):
        return cls(
            bool(config.TELEPORT_ENABLED), bool(config.TELEPORT_VFX_ENABLED),
            max(16, min(1000, int(config.TELEPORT_VFX_DURATION_MS))),
            max(0.0, min(3.0, float(config.TELEPORT_LIGHTNING_INTENSITY))),
            max(0, min(64, int(config.TELEPORT_SPARK_COUNT))),
        )


def clamp_to_displays(position, size, bounds):
    """Use the destination display, including negative origins and DPI islands."""
    if not bounds:
        return QPoint(position)
    desired = QRect(position, size)
    def score(rect):
        overlap = desired.intersected(rect)
        area = max(0, overlap.width()) * max(0, overlap.height())
        center = desired.center()
        dx = max(rect.left() - center.x(), 0, center.x() - rect.right())
        dy = max(rect.top() - center.y(), 0, center.y() - rect.bottom())
        return area, -(dx * dx + dy * dy)
    target = max(bounds, key=score)
    max_x = max(target.left(), target.right() - size.width() + 1)
    max_y = max(target.top(), target.bottom() - size.height() + 1)
    return QPoint(
        max(target.left(), min(position.x(), max_x)),
        max(target.top(), min(position.y(), max_y)),
    )


def _color(value, alpha):
    color = QColor(value)
    color.setAlpha(max(0, min(255, int(alpha))))
    return color


def _path(points):
    result = QPainterPath(points[0])
    for point in points[1:]:
        result.lineTo(point)
    return result


def _zigzag(rng, start, end, amount, steps=7):
    delta = end - start
    length = math.hypot(delta.x(), delta.y()) or 1
    normal = QPointF(-delta.y() / length, delta.x() / length)
    points = [start]
    for index in range(1, steps):
        along = (index + rng.uniform(-0.3, 0.3)) / steps
        offset = rng.uniform(0.35, 1.0) * amount * (-1 if index % 2 else 1)
        points.append(start + delta * along + normal * offset)
    points.append(end)
    return points


@dataclass(frozen=True)
class ElectricalArc:
    anchor: QPointF
    variants: tuple
    width: float
    phase: int


@dataclass(frozen=True)
class Spark:
    origin: QPointF
    velocity: QPointF
    direction: QPointF
    length: float
    phase: float


class LightningBurst:
    """All random geometry is built once; painting only changes its short lifetime."""

    def __init__(self, sprite_rect, silhouette, settings, started_ms, arrival, seed=None):
        self.sprite_rect = QRectF(sprite_rect)
        self.started_ms = started_ms
        self.duration_ms = settings.duration_ms
        self.arrival = arrival
        rng = random.Random(seed)
        scale = min(sprite_rect.width(), sprite_rect.height()) / 300
        strength = settings.intensity
        transform = QTransform()
        transform.translate(sprite_rect.x(), sprite_rect.y())
        transform.scale(sprite_rect.width(), sprite_rect.height())
        self.body = transform.map(silhouette.path)
        anchors = silhouette.anchors
        count = max(4, round((22 if arrival else 14) * strength))
        self.arcs = []
        extent = QRectF(sprite_rect)
        for index in range(count):
            # Spread roots over the actual hair/body contours, with irregular choices.
            anchor_index = (round(index * len(anchors) / count) + rng.randrange(4)) % len(anchors)
            point, normal = anchors[anchor_index]
            anchor = transform.map(point)
            tangent = QPointF(-normal.y(), normal.x())
            reach = rng.uniform(25, 72) * scale * (0.75 + strength * 0.25)
            width = rng.uniform(1.9, 3.0) * scale if index % 6 == 0 else rng.uniform(0.65, 1.35) * scale
            variants = []
            for flicker in range(3):
                start = normal * rng.uniform(1, 4) * scale
                if index % 4 == 0 and len(anchors) > 6:
                    next_point = anchors[(anchor_index + rng.randint(2, 7)) % len(anchors)][0]
                    end = transform.map(next_point) - anchor + normal * rng.uniform(5, 14) * scale
                else:
                    end = normal * reach + tangent * rng.uniform(-22, 22) * scale
                points = _zigzag(rng, start, end, rng.uniform(5, 12) * scale)
                paths = [_path(points)]
                if index % 3 != 1:
                    root = points[rng.randint(2, 5)]
                    branch_end = root + normal * reach * 0.5 + tangent * rng.choice((-1, 1)) * reach * 0.45
                    paths.append(_path(_zigzag(rng, root, branch_end, 4.5 * scale, 4)))
                for path in paths:
                    extent = extent.united(path.boundingRect().translated(anchor))
                variants.append(tuple(paths))
            self.arcs.append(ElectricalArc(anchor, tuple(variants), width, rng.randrange(3)))

        self.sparks = []
        spark_count = round(settings.spark_count * (1 if arrival else 0.65))
        for _ in range(spark_count):
            point, normal = rng.choice(anchors)
            tangent = QPointF(-normal.y(), normal.x())
            direction = normal + tangent * rng.uniform(-0.7, 0.7)
            magnitude = math.hypot(direction.x(), direction.y()) or 1
            direction /= magnitude
            origin = transform.map(point) + normal * 3 * scale
            velocity = direction * rng.uniform(25, 62) * scale
            length = rng.uniform(5, 16) * scale
            self.sparks.append(Spark(origin, velocity, direction, length, rng.random()))
            end = origin + velocity + direction * length
            extent = extent.united(QRectF(origin, end).normalized())
        self.bounds = extent.adjusted(-18 * scale, -18 * scale, 18 * scale, 18 * scale)
        outside = QPainterPath()
        outside.addRect(self.bounds)
        self.exterior = outside.subtracted(self.body)

    def paint(self, painter, now_ms):
        age = now_ms - self.started_ms
        if age < 0 or age >= self.duration_ms:
            return
        progress = age / self.duration_ms
        # Full energy from frame zero; only the electricity collapses/retracts.
        retract = 1.0 if progress < 0.18 else max(0, 1 - (progress - 0.18) / 0.82)
        energy = 1 - progress * 0.75
        flicker = int(age / 28)
        painter.save()
        painter.setClipPath(self.exterior, Qt.ClipOperation.IntersectClip)
        if not self.arrival and progress < 0.45:
            painter.save()
            painter.translate(-5 if flicker % 2 else 4, 0)
            painter.setPen(QPen(_color(PURPLE, 120 * energy), 3))
            painter.drawPath(self.body)
            painter.setPen(QPen(_color(CRIMSON, 160 * energy), 0.9))
            painter.drawPath(self.body)
            painter.restore()
        for index, arc in enumerate(self.arcs):
            if progress > 0.5 and (index + flicker) % 3 == 0:
                continue
            painter.save()
            painter.translate(arc.anchor)
            painter.scale(retract, retract)
            paths = arc.variants[(flicker + arc.phase) % len(arc.variants)]
            for branch_index, path in enumerate(paths):
                width = arc.width * (0.6 if branch_index else 1)
                for color, line_width, alpha in (
                    (PURPLE, width + 8, 55),
                    (CRIMSON, width + 4.2, 115),
                    (CRIMSON, width + 1.9, 245),
                    (BLACK, width, 255),
                ):
                    painter.setPen(QPen(
                        _color(color, alpha * energy), line_width,
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin,
                    ))
                    painter.drawPath(path)
                if index % 3 == 0:
                    painter.save()
                    painter.translate(0.9, -0.6)
                    painter.setPen(QPen(_color(NEON, 210 * energy), 0.65))
                    painter.drawPath(path)
                    painter.restore()
            painter.restore()
        for spark in self.sparks:
            life = min(1.0, progress * 1.5)
            if life >= 1:
                continue
            position = spark.origin + spark.velocity * life
            jitter = QPointF((flicker % 3 - 1) * 1.2, 0)
            end = position + spark.direction * spark.length * (1 - life)
            painter.setPen(QPen(_color(CRIMSON, 255 * (1 - life)), 2.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            painter.drawLine(position + jitter, end + jitter)
            painter.setPen(QPen(_color(NEON, 220 * (1 - life)), 0.7))
            painter.drawLine(position, end)
            if spark.phase > 0.6:
                # Sharp triangular splinters, never round particles.
                sideways = QPointF(-spark.direction.y(), spark.direction.x()) * 1.8
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_color(BLACK, 230 * (1 - life)))
                painter.drawPolygon(QPolygonF((position - sideways, end, position + sideways)))
                painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.restore()


class LightningOverlay(QWidget):
    def __init__(self, owner, screen, controller):
        super().__init__(owner, (
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint
        ))
        self.setObjectName("teleportLightningOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setScreen(screen)
        self.controller = controller
        self.viewport = QRect()
        self.capture_excluded = False
        # Native resources are warmed while hidden, never on the move path.
        handle = int(self.winId())
        if (os.name == "nt" and QGuiApplication.platformName() == "windows"
                and sys.getwindowsversion().build >= 19041):
            from ctypes import wintypes
            api = ctypes.WinDLL("user32", use_last_error=True)
            api.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            api.SetWindowDisplayAffinity.restype = wintypes.BOOL
            self.capture_excluded = bool(api.SetWindowDisplayAffinity(handle, 0x11))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(event.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(-self.viewport.x(), -self.viewport.y())
        self.controller.paint(painter, QRectF(self.viewport))
        painter.end()


class TeleportEffects(QObject):
    effects_changed = Signal()

    def __init__(self, owner, settings=None, *, protected_sprite=None):
        super().__init__(owner)
        self._owner_ref = weakref.ref(owner)
        self.settings = settings or TeleportSettings.from_config()
        self.bursts = []
        self._overlays = {}
        self._disposed = False
        self._protected_sprite = protected_sprite
        self._protected_key = None
        self._protected_body = QPainterPath()
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._app = QGuiApplication.instance()
        self._app.screenAdded.connect(self._screens_changed)
        self._app.screenRemoved.connect(self._screens_changed)
        self._screens_changed()

    def now_ms(self):
        return self._clock.nsecsElapsed() / 1_000_000

    @property
    def owner(self):
        return self._owner_ref()

    def paint(self, painter, viewport, now_ms=None):
        """Protect the current character from *all* bursts, including older ones."""
        if self._protected_sprite is not None:
            rect, silhouette = self._protected_sprite()
            key = (rect.x(), rect.y(), rect.width(), rect.height(), id(silhouette))
            if key != self._protected_key:
                self._protected_key = key
                self._protected_body = QPainterPath()
                if silhouette is not None:
                    transform = QTransform()
                    transform.translate(rect.x(), rect.y())
                    transform.scale(rect.width(), rect.height())
                    body = transform.map(silhouette.path)
                    # Cover subpixel edges lost when sampling the alpha mask.
                    stroker = QPainterPathStroker()
                    stroker.setWidth(4)
                    self._protected_body = body.united(stroker.createStroke(body))
        painter.save()
        outside = QPainterPath()
        outside.addRect(viewport)
        painter.setClipPath(outside.subtracted(self._protected_body), Qt.ClipOperation.IntersectClip)
        now = self.now_ms() if now_ms is None else now_ms
        for burst in self.bursts:
            if burst.bounds.intersects(viewport):
                burst.paint(painter, now)
        painter.restore()

    @property
    def active_windows(self):
        return tuple(overlay for overlay in self._overlays.values() if overlay.isVisible())

    @property
    def capture_windows(self):
        return tuple(overlay for overlay in self.active_windows if not overlay.capture_excluded)

    def _screens_changed(self, *args):
        if self._disposed:
            return
        screens = QGuiApplication.screens()
        for screen in list(self._overlays):
            if screen not in screens:
                overlay = self._overlays.pop(screen)
                overlay.hide()
                overlay.deleteLater()
                try:
                    screen.geometryChanged.disconnect(self._geometry_changed)
                except (RuntimeError, TypeError):
                    pass
        for screen in screens:
            if screen not in self._overlays:
                self._overlays[screen] = LightningOverlay(self.owner, screen, self)
                screen.geometryChanged.connect(self._geometry_changed)
        self._update_windows()
        self.effects_changed.emit()

    def _geometry_changed(self, *args):
        self._update_windows()
        self.effects_changed.emit()

    def play(self, departure_rect, arrival_rect, silhouette, arrival_silhouette=None):
        settings = self.settings
        if (self._disposed or not settings.enabled or not settings.vfx_enabled
                or settings.intensity <= 0 or silhouette is None):
            return
        started = self.now_ms()
        self.bursts = [burst for burst in self.bursts if started - burst.started_ms < burst.duration_ms]
        self.bursts.extend((
            LightningBurst(departure_rect, silhouette, settings, started, False),
            LightningBurst(arrival_rect, arrival_silhouette or silhouette, settings, started, True),
        ))
        self.bursts = self.bursts[-MAX_ACTIVE_BURSTS:]
        self._update_windows()
        self._timer.start()
        self.effects_changed.emit()

    def _update_windows(self):
        for screen, overlay in self._overlays.items():
            viewport = QRect()
            bounds = screen.geometry()
            for burst in self.bursts:
                clipped = burst.bounds.toAlignedRect().intersected(bounds)
                if not clipped.isEmpty():
                    viewport = viewport.united(clipped)
            if viewport.isEmpty():
                overlay.hide()
                continue
            overlay.viewport = viewport
            if overlay.geometry() != viewport:
                overlay.setGeometry(viewport)
            if not overlay.isVisible():
                overlay.show()
                overlay.raise_()
            overlay.update()

    def _tick(self):
        now = self.now_ms()
        remaining = [burst for burst in self.bursts if now - burst.started_ms < burst.duration_ms]
        expired = len(remaining) != len(self.bursts)
        self.bursts = remaining
        if expired:
            self._update_windows()
            self.effects_changed.emit()
        else:
            for overlay in self.active_windows:
                overlay.update()
        if not self.bursts:
            self._timer.stop()

    def stop(self):
        self._timer.stop()
        self.bursts.clear()
        for overlay in self._overlays.values():
            overlay.hide()
        self.effects_changed.emit()

    def dispose(self):
        if self._disposed:
            return
        self.stop()
        self._disposed = True
        for signal in (self._app.screenAdded, self._app.screenRemoved):
            try:
                signal.disconnect(self._screens_changed)
            except (RuntimeError, TypeError):
                pass
        for screen, overlay in self._overlays.items():
            try:
                screen.geometryChanged.disconnect(self._geometry_changed)
            except (RuntimeError, TypeError):
                pass
            overlay.hide()
            overlay.deleteLater()
        self._overlays.clear()
        self._protected_sprite = None

"""GUI action coordination and a short-lived, click-through target highlight."""

import time
import weakref
from threading import Event, Thread

from PySide6.QtCore import QElapsedTimer, QObject, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainterPath, QPen, QTransform

import config
from core.spatial import ActionState, MovementIntent
from core.spatial_geometry import Rect, place_beside
from core.window_grounding import visible_parts
from gui.teleport import LightningOverlay
from gui.target_lightning import TargetLightning


class TargetHighlight(QObject):
    changed = Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self._owner_ref = weakref.ref(owner)
        self.rect = QRectF()
        self._overlay = None
        self._screen = None
        self._clock = QElapsedTimer()
        self._duration = 0
        self.beam = None
        self._channel = False
        self._endpoint = None
        self._body = QPainterPath()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    @property
    def owner(self):
        return self._owner_ref()

    @property
    def capture_windows(self):
        if self._overlay and self._overlay.isVisible() and not self._overlay.capture_excluded:
            return (self._overlay,)
        return ()

    def show_target(self, target, monitor, duration_ms=None, *, channel=False):
        self.clear()
        self._channel = channel and config.TARGET_LIGHTNING_ENABLED
        if not config.TARGET_HIGHLIGHT_ENABLED and not self._channel:
            return
        screen = next((screen for screen in QGuiApplication.screens()
                       if screen.geometry() == QRect(round(monitor.logical.x), round(monitor.logical.y),
                                                     round(monitor.logical.width), round(monitor.logical.height))), None)
        screen = screen or QGuiApplication.screenAt(QPoint(round(monitor.logical.center[0]), round(monitor.logical.center[1])))
        if screen is None:
            return
        if self._overlay is None or self._screen is not screen:
            if self._overlay:
                self._overlay.deleteLater()
            self._overlay = LightningOverlay(self.owner, screen, self)
            self._overlay.setObjectName("spatialTargetHighlight")
            self._screen = screen
        box = target.bounding_box
        self.rect = QRectF(box.x, box.y, box.width, box.height).adjusted(-4, -4, 4, 4)
        self._endpoint = self.rect.center()
        if not self._update_geometry():
            return
        self._duration = max(200, min(10000, duration_ms or config.TARGET_HIGHLIGHT_DURATION_MS))
        self._clock.start()
        self._overlay.show()
        self._overlay.raise_()
        self._overlay.update()
        self._timer.start()
        self.changed.emit()

    def _update_geometry(self):
        if self._channel:
            hand = self.owner.sprite.pointing_hand_global(self.owner)
            if hand is None:
                self.clear()
                return False
            if self.beam is None or self.beam.start != hand or self.beam.end != self._endpoint:
                self.beam = TargetLightning(hand, self._endpoint, config.TARGET_LIGHTNING_INTENSITY)
                rect = self.owner._sprite_screen_rect()
                transform = QTransform()
                transform.translate(rect.x(), rect.y())
                transform.scale(rect.width(), rect.height())
                shape = self.owner.sprite.teleport_silhouette()
                self._body = transform.map(shape.path) if shape else QPainterPath()
        bounds = self.rect.adjusted(-5, -5, 5, 5)
        if self.beam is not None:
            bounds = bounds.united(self.beam.bounds)
        viewport = bounds.toAlignedRect().intersected(self._screen.geometry())
        if viewport.isEmpty():
            self.clear()
            return False
        if viewport != self._overlay.viewport:
            self._overlay.viewport = viewport
            self._overlay.setGeometry(viewport)
            self.changed.emit()
        return True

    def now_ms(self):
        return self._clock.elapsed() if self._clock.isValid() else 0

    def paint(self, painter, viewport):
        painter.save()
        outside = QPainterPath()
        outside.addRect(viewport)
        painter.setClipPath(outside.subtracted(self._body), Qt.ClipOperation.IntersectClip)
        if self.beam is not None:
            self.beam.paint(painter, self.now_ms())
        if config.TARGET_HIGHLIGHT_ENABLED and self.now_ms() < self._duration:
            self._paint_outline(painter)
        painter.restore()

    def _paint_outline(self, painter):
        alpha = min(1, max(0, (self._duration - self.now_ms()) / 250))
        color = QColor("#FF0033")
        color.setAlpha(round(240 * alpha))
        painter.setPen(QPen(QColor(255, 0, 51, round(65 * alpha)), 1))
        painter.drawRect(self.rect)
        length = min(22, self.rect.width() / 3, self.rect.height() / 3)
        for corner, dx, dy in ((self.rect.topLeft(), 1, 1), (self.rect.topRight(), -1, 1),
                               (self.rect.bottomLeft(), 1, -1), (self.rect.bottomRight(), -1, -1)):
            painter.setPen(QPen(QColor(9, 0, 9, round(210 * alpha)), 4))
            end_x = corner + type(corner)(dx * length, 0)
            end_y = corner + type(corner)(0, dy * length)
            painter.drawLine(corner, end_x)
            painter.drawLine(corner, end_y)
            painter.setPen(QPen(color, 1.6))
            painter.drawLine(corner, end_x)
            painter.drawLine(corner, end_y)

    def _tick(self):
        if self._channel:
            if self._update_geometry():
                self._overlay.update()
        elif self.now_ms() >= self._duration:
            self.clear()
        elif self._overlay:
            self._overlay.update()

    def clear(self):
        self._timer.stop()
        self._channel = False
        self.beam = None
        self._body = QPainterPath()
        if self._overlay:
            self._overlay.hide()
        self.changed.emit()

    def dispose(self):
        self.clear()
        if self._overlay:
            self._overlay.deleteLater()
            self._overlay = None
        self._screen = None


class MovementCoordinator(QObject):
    requested = Signal(object)
    validity_checked = Signal(int, str, bool)
    state_changed = Signal()

    def __init__(self, owner, monitors, grounder):
        super().__init__(owner)
        self._owner_ref = weakref.ref(owner)
        self.monitors, self.grounder = monitors, grounder
        self.state = ActionState.IDLE
        self.intent = MovementIntent.IDLE
        self.target = None
        self.generation = 0
        self.previous_position = None
        self.highlight = TargetHighlight(owner)
        self.requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)
        self.validity_checked.connect(self._validity_result)
        self._expires = QTimer(self)
        self._expires.setSingleShot(True)
        self._expires.timeout.connect(self.reset)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self.reset)
        self._watch = QTimer(self)
        self._watch.setInterval(300)
        self._watch.timeout.connect(self._check_target)
        self._watch_pending = False
        self._watch_cancel = Event()
        self._disposed = False
        self._pointed_at = 0.0

    @property
    def owner(self):
        return self._owner_ref()

    @property
    def busy(self):
        return self.state != ActionState.IDLE

    def dispatch(self, ticket):
        if self._disposed:
            ticket.result = {"status": "cancelled", "message": "Capri is closing"}
            ticket.done.set()
            return
        self.requested.emit(ticket)

    def reset(self):
        self._expires.stop()
        self._hold.stop()
        self._watch.stop()
        self._watch_cancel.set()
        self._watch_cancel = Event()
        self.highlight.clear()
        if self.owner.sprite.pointing_direction:
            self.owner.sprite.end_pointing()
        self.target = None
        self.state = ActionState.IDLE
        self.intent = MovementIntent.IDLE
        self.owner._next_wander_at = time.monotonic() + 3
        self.state_changed.emit()

    def _execute(self, ticket):
        try:
            if (self._disposed or ticket.generation < self.generation
                    or (ticket.cancellation.is_set() and ticket.kind not in ("cancel", "finish"))):
                ticket.result = {"status": "cancelled", "message": "A newer request cancelled this action"}
                return
            if ticket.generation > self.generation:
                self.reset()
                self.generation = ticket.generation
            if ticket.kind == "cancel":
                self.reset()
            elif ticket.kind == "search":
                self.reset()
                self.state, self.intent = ActionState.SEARCHING, MovementIntent.LOCATE
            elif ticket.kind == "finish":
                if ticket.cancellation.is_set():
                    self.reset()
                    return
                remaining = config.SPATIAL_POINTING_HOLD_MS - round((time.monotonic() - self._pointed_at) * 1000)
                if self.target and remaining > 0:
                    self._hold.start(remaining)
                else:
                    self.reset()
            elif ticket.kind == "speaking":
                self.speaking()
            elif ticket.kind == "return":
                self.reset()
                self.intent = MovementIntent.RETURN
                if self.previous_position is None:
                    ticket.result = {"status": "unavailable", "message": "There is no previous contextual position"}
                else:
                    self.owner.teleport_to(self.previous_position)
                    ticket.result = {"status": "returned"}
                self.intent = MovementIntent.IDLE
            elif ticket.kind == "show":
                self._show(ticket)
        except Exception as error:
            self.reset()
            ticket.result = {"status": "failed", "message": str(error)}
        finally:
            self.state_changed.emit()
            ticket.done.set()

    def _show(self, ticket):
        target = ticket.target
        if target is None or target.revision != self.monitors.revision or time.monotonic() - target.timestamp > 1.5:
            raise ValueError("Target expired or display layout changed before arrival")
        if not self.owner.context.spatial.enabled():
            raise ValueError("Screen awareness was turned off before arrival")
        monitor = self.monitors.resolve(target.monitor_id)
        if target.window_handle and not self.grounder.unchanged(target.window_handle, target.window_pid, target.window_bounds):
            raise ValueError("Target window moved or disappeared before arrival")
        if not monitor.logical.contains(target.bounding_box):
            raise ValueError("Target is outside the connected display")
        self.state = ActionState.TARGET_FOUND
        self.intent = MovementIntent.POINT
        metrics = self.owner.sprite.pointing_metrics(self.owner)
        obstacles = []
        for widget in (self.owner.chat_panel, self.owner.controls_popup, self.owner.advice_bubble):
            if widget.isVisible():
                rect = widget.frameGeometry()
                obstacles.append(Rect(rect.x(), rect.y(), rect.width(), rect.height()))
        placement = place_beside(target.bounding_box, monitor.available, metrics, obstacles)
        if self.target is None:
            self.previous_position = QPoint(self.owner.pos())
        self.state = ActionState.TELEPORTING
        if "pointingleft" not in self.owner.sprite.sprite_names:
            raise ValueError("pointingLeft.svg is missing or invalid")
        def pose():
            self.owner.sprite.start_pointing(placement.direction)
        if ticket.cancellation.is_set():
            raise ValueError("A newer request cancelled this target")
        self.owner.teleport_to(placement.x, placement.y, arrival_pose=pose)
        self.owner.sprite.update()
        self.target = target
        self.state = ActionState.POINTING
        self._pointed_at = time.monotonic()
        self._hold.stop()
        self._expires.start(ticket.duration_ms or config.SPATIAL_POINTING_MAX_MS)
        self.highlight.show_target(target, monitor, channel=True)
        self._watch.start()
        ticket.result = {"status": "pointing", "target": target.to_dict(), "monitor_number": monitor.number,
                         "direction": placement.direction, "placement": {"x": placement.x, "y": placement.y},
                         "partly_overlaps_target": placement.overlap > 0,
                         "message": "Capri has arrived and is pointing. Now explain the verified target naturally."}

    def speaking(self):
        if self.target:
            self.state = ActionState.SPEAKING
            self._expires.stop()  # Speech owns the pose until finish/Stop/new input.

    def _check_target(self):
        if not self.target or self._watch_pending or self._disposed:
            return
        if self.owner.sprite.pointing_direction is None:
            self.reset()
            return
        target, generation, cancellation = self.target, self.generation, self._watch_cancel
        self._watch_pending = True
        def check():
            valid = target.revision == self.monitors.revision
            try:
                if target.window_handle:
                    windows = self.grounder.windows(cancellation)
                    window = next((window for window in windows if window.handle == target.window_handle
                                   and window.pid == target.window_pid), None)
                    valid = valid and window is not None and window.bounds == target.window_bounds
                    if valid:
                        parts = visible_parts(target.physical_box, window, windows)
                        valid = sum(part.area for part in parts) >= target.physical_box.area * 0.85
                else:
                    valid = valid and time.monotonic() - target.timestamp < 5
            except Exception:
                valid = False
            if not self._disposed:
                try:
                    self.validity_checked.emit(generation, target.id, valid)
                except RuntimeError:
                    pass
        Thread(target=check, name="Capri target visibility", daemon=True).start()

    def _validity_result(self, generation, target_id, valid):
        self._watch_pending = False
        if generation == self.generation and self.target and self.target.id == target_id and not valid:
            self.reset()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self.reset()
        self.highlight.dispose()

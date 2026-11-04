import logging
import math
import re
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPoint, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from gui.silhouette import SpriteSilhouette, silhouette_from_renderer
from core.spatial_geometry import Rect, SpriteMetrics
from config import POINTING_LEFT_HAND_ANCHOR


SPRITE_DIRECTORY = Path(__file__).resolve().parents[1] / "assets" / "sprites"
ANIMATION_INTERVALS = {
    "idle": 100,
    "listening": 850,
    "speech_detected": 180,
    "processing": 125,
    "speaking": 180,
    "playful": 500,
    "happy": 500,
    "pointing_left": 500,
}
ACTIVITY_KEYWORDS = {
    "idle": (
        "idle", "idling", "breath", "breathing", "wait", "waiting",
        "standby", "neutral", "rest",
    ),
    "listening": ("listen", "listening", "hear", "hearing"),
    "speech_detected": ("speechdetected", "detected"),
    "processing": (
        "process", "processing", "think", "thinking", "glitch", "netrun",
        "hack", "hacking", "analyze", "analysing", "analyzing",
        "disbelief", "confused",
    ),
    "speaking": (
        "speak", "speaking", "talk", "talking", "reply",
    ),
    "playful": ("playful", "playfull", "tease", "teasing"),
    "happy": ("happy", "joy", "excited"),
    "pointing_left": ("pointingleft", "pointing"),
}
ACTIVITY_ALIASES = {
    "speech": "speech_detected",
    "speechdetected": "speech_detected",
    "idling": "idle",
    "waiting": "idle",
    "standby": "idle",
    "thinking": "processing",
    "transcribing": "processing",
    "loading": "processing",
    "talking": "speaking",
    "speaking": "speaking",
    "playfull": "playful",
    "teasing": "playful",
    "excited": "happy",
    "pointing": "pointing_left",
    "pointingleft": "pointing_left",
}


def _normalize_name(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class CharacterSprite(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._renderers = {}
        self._file_signatures = {}
        self._silhouettes = {}
        self._state = "idle"
        self._activity_name = "idle"
        self._activity_tokens = ("idle",)
        self._frame_index = 0
        self._animation_ticks = 0
        self._elapsed = 0
        self._frames = ()
        self._expression = None
        self._pointing = None
        self._pointing_scale = 1.0
        self._pointing_shapes = {}
        self._expression_timer = QTimer(self)
        self._expression_timer.setSingleShot(True)
        self._expression_timer.timeout.connect(self._restore_activity)

        self._watcher = QFileSystemWatcher(self)
        if SPRITE_DIRECTORY.is_dir():
            self._watcher.addPath(str(SPRITE_DIRECTORY))
        self._watcher.directoryChanged.connect(self._reload_sprites)
        self._watcher.fileChanged.connect(self._reload_sprites)
        self._reload_sprites()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._timer.start(ANIMATION_INTERVALS["idle"])
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.set_activity("idle")

    @property
    def state(self):
        return self._state

    @property
    def sprite_names(self):
        """Return the names of all currently loaded SVG files."""
        return tuple(sorted(self._renderers))

    def set_activity(self, activity):
        """Select sprites by activity or by the name of an SVG in the sprite folder."""
        name = Path(activity).stem
        normalized = _normalize_name(name)
        if not normalized:
            raise ValueError("Sprite activity must have a name.")

        tokens = normalized.split()
        state = ACTIVITY_ALIASES.get(tokens[0], tokens[0])
        self._state = state
        self._activity_name = normalized
        self._activity_tokens = tuple(tokens)
        self._frame_index = 0
        self._animation_ticks = 0
        self._elapsed = 0
        if state != "idle":
            self._expression = None
            self._expression_timer.stop()
        self._frames = (
            (self._expression,) if self._expression in self._renderers
            else self._find_frames(state, normalized, tokens)
        )
        if self._pointing and "pointingleft" in self._renderers:
            self._frames = ("pointingleft",)
        self._timer.setInterval(ANIMATION_INTERVALS.get(state, 500))
        self.update()

    @property
    def expression(self):
        return self._expression

    def flash_expression(self, name, duration_ms=1100):
        """A brief idle pose; voice/listening/processing expressions take priority."""
        name = _normalize_name(Path(name).stem)
        if self._state != "idle" or self._pointing or name not in self._renderers:
            return False
        self._expression = name
        self._frames = (name,)
        self._frame_index = 0
        self._expression_timer.start(max(1, duration_ms))
        self.update()
        return True

    def _restore_activity(self):
        self._expression = None
        self._frames = (("pointingleft",) if self._pointing else
                        self._find_frames(self._state, self._activity_name, self._activity_tokens))
        self._frame_index = 0
        self.update()

    def teleport_silhouette(self):
        """Only cached data is read on the teleport path."""
        if not self._frames:
            return None
        if self._pointing:
            return self._pointing_shape(self._pointing)
        return self._silhouettes.get(self._frames[self._frame_index])

    @property
    def pointing_direction(self):
        return self._pointing

    @staticmethod
    def _feet(shape):
        bottom = shape.path.boundingRect().bottom()
        feet = [point for point, normal in shape.anchors if point.y() >= bottom - 0.1]
        return QPointF((min(point.x() for point in feet) + max(point.x() for point in feet)) / 2, bottom)

    def _pointing_transform(self, direction):
        pose = self._silhouettes["pointingleft"]
        reference = self._silhouettes.get("idle", pose)
        feet, reference_feet = self._feet(pose), self._feet(reference)
        if direction == "right":
            return QTransform(-1, 0, 0, 1, reference_feet.x() + feet.x(), reference_feet.y() - feet.y())
        return QTransform.fromTranslate(reference_feet.x() - feet.x(), reference_feet.y() - feet.y())

    def _pointing_shape(self, direction):
        if direction not in self._pointing_shapes:
            pose = self._silhouettes["pointingleft"]
            transform = self._pointing_transform(direction)
            sign = -1 if direction == "right" else 1
            self._pointing_shapes[direction] = SpriteSilhouette(
                transform.map(pose.path), tuple((transform.map(point), QPointF(sign * normal.x(), normal.y()))
                                                for point, normal in pose.anchors),
            )
        return self._pointing_shapes[direction]

    def start_pointing(self, direction):
        if direction not in ("left", "right") or "pointingleft" not in self._renderers:
            return False
        if not self._pointing:
            self._pointing_scale = self.render_target().width() / max(1, min(self.width(), self.height()))
        self._pointing = direction
        self._expression = None
        self._expression_timer.stop()
        self._frames = ("pointingleft",)
        self._frame_index = 0
        self.update()
        return True

    def end_pointing(self):
        self._pointing = None
        self._restore_activity()

    def pointing_metrics(self, window):
        if "pointingleft" not in self._renderers:
            raise ValueError("pointingLeft.svg is unavailable")
        target = self.render_target()
        origin = self.mapTo(window, QPoint(0, 0))
        transform = QTransform()
        transform.translate(origin.x() + target.x(), origin.y() + target.y())
        transform.scale(target.width(), target.height())
        left = self._pointing_shape("left")
        right = self._pointing_shape("right")
        bounds = transform.map(left.path).boundingRect().united(transform.map(right.path).boundingRect())
        hand = QPointF(*POINTING_LEFT_HAND_ANCHOR)
        left_hand = transform.map(self._pointing_transform("left").map(hand))
        right_hand = transform.map(self._pointing_transform("right").map(hand))
        return SpriteMetrics(Rect(bounds.x(), bounds.y(), bounds.width(), bounds.height()),
                             (left_hand.x(), left_hand.y()), (right_hand.x(), right_hand.y()),
                             (window.width(), window.height()))

    def pointing_hand_global(self, window):
        """Match the rendered pose, including mirroring, feet alignment and scale."""
        if self._pointing is None:
            return None
        metrics = self.pointing_metrics(window)
        hand = metrics.left_hand if self._pointing == "left" else metrics.right_hand
        return QPointF(window.mapToGlobal(QPoint(0, 0))) + QPointF(*hand)

    def draw_character(self, painter, target):
        if self._pointing:
            painter.save()
            painter.translate(target.x(), target.y())
            painter.scale(target.width(), target.height())
            painter.setTransform(self._pointing_transform(self._pointing), True)
            self._renderers["pointingleft"].render(painter, QRectF(0, 0, 1, 1))
            painter.restore()
            return
        if self._frames:
            self._renderers[self._frames[self._frame_index]].render(painter, target)

    def render_target(self):
        side = min(self.width(), self.height())
        scale = 1 + 0.012 * math.sin(self._elapsed / 650) if self._state == "idle" else 1.0
        if self._pointing:
            scale = self._pointing_scale
        render_side = side * scale
        return QRectF(
            (self.width() - render_side) / 2, (self.height() - render_side) / 2,
            render_side, render_side,
        )

    def set_state(self, state):
        """Compatibility alias for callers that describe an activity as a state."""
        self.set_activity(state)

    def stop_animation(self):
        self._timer.stop()
        self._expression_timer.stop()
        self._watcher.blockSignals(True)

    def _reload_sprites(self, changed_path=None):
        if (
            changed_path
            and Path(changed_path).suffix.lower() not in ("", ".svg")
        ):
            return

        if SPRITE_DIRECTORY.is_dir() and str(SPRITE_DIRECTORY) not in self._watcher.directories():
            self._watcher.addPath(str(SPRITE_DIRECTORY))

        paths = {
            path.resolve(): path
            for path in SPRITE_DIRECTORY.glob("*.svg")
            if path.is_file()
        } if SPRITE_DIRECTORY.is_dir() else {}
        loaded = {}
        signatures = {}
        silhouettes = {}
        watched_files = set(self._watcher.files())

        for resolved_path, path in paths.items():
            name = _normalize_name(path.stem)
            if not name:
                continue

            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                renderer = self._renderers.get(name)
                if self._file_signatures.get(name) != signature:
                    renderer = QSvgRenderer(str(path), self)
                    if not renderer.isValid():
                        logging.warning("Ignoring invalid Capri sprite: %s", path)
                        renderer.deleteLater()
                        continue
                if renderer is None:
                    logging.warning("Could not load Capri sprite: %s", path)
                    continue
                silhouette = self._silhouettes.get(name)
                if silhouette is None or self._file_signatures.get(name) != signature:
                    silhouette = silhouette_from_renderer(renderer)
            except OSError:
                logging.warning("Could not read Capri sprite: %s", path, exc_info=True)
                continue

            loaded[name] = renderer
            signatures[name] = signature
            silhouettes[name] = silhouette
            resolved_string = str(resolved_path)
            if resolved_string not in watched_files:
                self._watcher.addPath(resolved_string)

        current_files = set(paths)
        for watched_path in watched_files:
            if Path(watched_path).resolve() not in current_files:
                self._watcher.removePath(watched_path)

        for name, renderer in self._renderers.items():
            if loaded.get(name) is not renderer:
                renderer.deleteLater()
        self._renderers = loaded
        self._file_signatures = signatures
        self._silhouettes = silhouettes
        self._pointing_shapes.clear()
        if "pointingleft" not in loaded:
            self._pointing = None
        if not self._renderers:
            logging.error("No valid SVG sprites found in %s", SPRITE_DIRECTORY)
            self._frames = ()
        else:
            if self._expression not in self._renderers:
                self._expression = None
            self._frames = (self._expression,) if self._expression else self._find_frames(
                self._state, self._activity_name, self._activity_tokens
            )
        self._frame_index = 0
        if self._pointing:
            self._frames = ("pointingleft",)
        self.update()

    def _find_frames(self, state, requested_name, requested_tokens):
        names = tuple(sorted(self._renderers))
        if not names:
            return ()

        keywords = ACTIVITY_KEYWORDS.get(state, ())
        matches = tuple(
            name for name in names
            if any(
                token == keyword
                or (len(keyword) >= 5 and token.startswith(keyword))
                for token in _normalize_name(name).split()
                for keyword in keywords
            )
        )
        if matches:
            return matches

        exact = tuple(name for name in names if name == requested_name)
        if exact:
            return exact

        requested_matches = tuple(
            name for name in names
            if any(token in _normalize_name(name).split() for token in requested_tokens)
        )
        if requested_matches:
            return requested_matches

        fallback = next((name for name in ("idle", "listening") if name in names), None)
        return (fallback or names[0],)

    def _advance(self):
        self._animation_ticks += 1
        if self._state == "idle":
            self._elapsed += ANIMATION_INTERVALS["idle"]
        elif len(self._frames) > 1:
            self._frame_index = (self._frame_index + 1) % len(self._frames)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        target = self.render_target()
        self.draw_character(painter, target)

        if self._state == "processing" and not self._pointing:
            self._paint_data_glitch(painter, target)
        event.accept()

    def _paint_data_glitch(self, painter, target):
        color = QColor("#53e8f4")
        color.setAlpha(85)
        painter.setPen(QPen(color, 1))

        scan_y = target.top() + (self._animation_ticks * 29) % max(
            1, int(target.height())
        )
        painter.drawLine(
            int(target.left() + target.width() * 0.12),
            int(scan_y),
            int(target.left() + target.width() * 0.88),
            int(scan_y),
        )

        offset = (self._animation_ticks * 17) % max(1, int(target.width() * 0.35))
        painter.drawLine(
            int(target.left() + offset),
            int(target.top() + target.height() * 0.24),
            int(target.left() + offset + target.width() * 0.08),
            int(target.top() + target.height() * 0.24),
        )

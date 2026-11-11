import logging
import math
import re
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPoint, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QBitmap, QColor, QImage, QPainter, QPen, QRegion, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from gui.silhouette import SpriteSilhouette, silhouette_from_renderer
from core.spatial_geometry import Rect, SpriteMetrics
from config import POINTING_LEFT_HAND_ANCHOR
import config


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
        self._league_mode = False
        self._league_speaking = False
        self._league_names = ()
        self._league_signature = None
        self._league_bounds = QRectF()
        self._league_transforms = {}
        self._league_shapes = {}
        self._league_warning = False
        self._league_available = (float("inf"), float("inf"))
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
        if self._league_mode:
            self._apply_league_frame()
        self.update()

    @property
    def league_mode(self):
        return self._league_mode

    def set_league_mode(self, active):
        active = bool(active)
        if self._league_mode == active:
            return
        self._league_mode = active
        self._expression = None
        self._expression_timer.stop()
        self._pointing = None
        if active:
            self._apply_league_frame()
            if self._league_names:
                self._timer.stop()
        else:
            self._restore_activity()
            self._timer.start(ANIMATION_INTERVALS.get(self._state, 500))
        self.update()

    def set_league_speaking(self, speaking):
        speaking = bool(speaking)
        if speaking != self._league_speaking:
            self._league_speaking = speaking
            if self._league_mode:
                self._apply_league_frame()
                self.update()

    def _apply_league_frame(self):
        if self._league_names:
            self._frames = (self._league_names[int(self._league_speaking)],)
            self._frame_index = 0

    def league_visible_rect(self):
        """Shared visible-artwork coordinates inside this widget (Qt logical)."""
        if self._league_names:
            return self._league_canvas()
        return QRectF(self.rect())  # Safe fallback envelope for the normal sprites.

    def set_league_available_size(self, width, height):
        size = (max(1, width), max(1, height))
        if size != self._league_available:
            self._league_available = size
            if self._league_mode:
                self.update()

    def _league_canvas(self):
        scale = min(min(self.width(), self._league_available[0]) / self._league_bounds.width(),
                    min(self.height(), self._league_available[1]) / self._league_bounds.height())
        width, height = self._league_bounds.width() * scale, self._league_bounds.height() * scale
        return QRectF(self.width() - width, (self.height() - height) / 2, width, height)

    @property
    def expression(self):
        return self._expression

    def flash_expression(self, name, duration_ms=1100):
        """A brief idle pose; voice/listening/processing expressions take priority."""
        name = _normalize_name(Path(name).stem)
        if self._league_mode or self._state != "idle" or self._pointing or name not in self._renderers:
            return False
        self._expression = name
        self._frames = (name,)
        self._frame_index = 0
        self._expression_timer.start(max(1, duration_ms))
        self.update()
        return True

    def react(self, name, duration_ms=750):
        """Briefly react during speech without changing the activity or pointing pose."""
        name = _normalize_name(name)
        if self._league_mode or self._pointing or self._state not in ("idle", "speaking") or name not in self._renderers:
            return False
        self._expression = name
        self._frames = (name,)
        self._frame_index = 0
        self._expression_timer.start(max(1, duration_ms))
        self.update()
        return True

    def clear_reaction(self):
        self._expression_timer.stop()
        self._restore_activity()

    def _restore_activity(self):
        self._expression = None
        self._frames = (("pointingleft",) if self._pointing else
                        self._find_frames(self._state, self._activity_name, self._activity_tokens))
        self._frame_index = 0
        if self._league_mode:
            self._apply_league_frame()
            if self._league_names and hasattr(self, "_timer"):
                self._timer.stop()
        self.update()

    def teleport_silhouette(self):
        """Only cached data is read on the teleport path."""
        if not self._frames:
            return None
        if self._league_mode and self._league_names:
            return self._league_shapes.get(self._frames[0])
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
        if self._league_mode or direction not in ("left", "right") or "pointingleft" not in self._renderers:
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
        if self._league_mode and self._league_names:
            name = self._frames[0]
            painter.save()
            painter.translate(target.x(), target.y())
            painter.scale(target.width(), target.height())
            painter.setTransform(self._league_transforms[name], True)
            self._renderers[name].render(painter, self._renderers[name].viewBoxF())
            painter.restore()
            return
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
        if self._league_mode and self._league_names:
            return self._league_canvas()
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
        self._cache_league_layout()
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
        if self._league_mode:
            self._apply_league_frame()
        self.update()

    def _cache_league_layout(self):
        names = tuple(_normalize_name(Path(name).stem) for name in
                      (config.LEAGUE_LOOKING_SPRITE, config.LEAGUE_TALKING_SPRITE))
        signature = tuple((name, self._file_signatures.get(name)) for name in names)
        if signature == self._league_signature:
            return
        self._league_signature = signature
        self._league_names = ()
        self._league_transforms.clear()
        self._league_shapes.clear()
        if any(name not in self._renderers for name in names):
            if not self._league_warning:
                logging.warning("League companion SVGs unavailable; keeping Capri's normal sprites (%s, %s)",
                                config.LEAGUE_LOOKING_SPRITE, config.LEAGUE_TALKING_SPRITE)
                self._league_warning = True
            return
        bounds = {}
        for name in names:
            renderer = self._renderers[name]
            view = renderer.viewBoxF()
            # Match actual SVG aspect ratio; only alpha measurement is rasterized,
            # once on load. Painting always uses the cached vector renderer.
            factor = 1024 / max(1, view.width(), view.height())
            width, height = max(1, math.ceil(view.width() * factor)), max(1, math.ceil(view.height() * factor))
            image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            renderer.render(painter, QRectF(0, 0, width, height))
            painter.end()
            alpha = QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
            if alpha.isEmpty():
                logging.warning("League companion SVG has no visible artwork: %s", name)
                return
            # Normalize SVG units by viewBox height, rather than assuming both
            # exports use identical origins, dimensions or pixel units.
            bounds[name] = QRectF(alpha.x() * view.width() / width / view.height(),
                                 alpha.y() / height,
                                 alpha.width() * view.width() / width / view.height(), alpha.height() / height)
        shared = bounds[names[0]].united(bounds[names[1]])
        self._league_bounds = shared
        for name in names:
            body = bounds[name]
            dx, dy = shared.right() - body.right(), shared.bottom() - body.bottom()
            view = self._renderers[name].viewBoxF()
            transform = QTransform(1 / view.height() / shared.width(), 0, 0, 1 / view.height() / shared.height(),
                                   (dx - shared.x() - view.x() / view.height()) / shared.width(),
                                   (dy - shared.y() - view.y() / view.height()) / shared.height())
            self._league_transforms[name] = transform
            shape = self._silhouettes[name]
            native = QTransform(view.width(), 0, 0, view.height(), view.x(), view.y())
            mapped = native * transform
            self._league_shapes[name] = SpriteSilhouette(mapped.map(shape.path),
                tuple((mapped.map(point), normal) for point, normal in shape.anchors))
        self._league_names = names
        self._league_warning = False

    def _find_frames(self, state, requested_name, requested_tokens):
        league_names = {_normalize_name(Path(name).stem) for name in
                        (config.LEAGUE_LOOKING_SPRITE, config.LEAGUE_TALKING_SPRITE)}
        names = tuple(name for name in sorted(self._renderers) if name not in league_names)
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
        if self._league_mode and self._league_names:
            return  # No breathing scale, expressions or animation frame changes.
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

        if self._state == "processing" and not self._pointing and not self._league_mode:
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

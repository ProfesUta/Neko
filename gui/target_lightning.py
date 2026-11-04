"""Cached, sharp electrical paths joining two measured desktop positions."""

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPen

from gui.teleport import BLACK, CRIMSON, NEON, PURPLE, _color, _path, _zigzag


class TargetLightning:
    """Global logical coordinates; endpoints never jitter or come from model output."""

    def __init__(self, start, end, intensity=1.0, seed=0):
        self.start, self.end = QPointF(start), QPointF(end)
        self.intensity = max(0.1, min(3.0, float(intensity)))
        delta = self.end - self.start
        length = math.hypot(delta.x(), delta.y())
        normal = QPointF(-delta.y(), delta.x()) / max(1, length)
        amplitude = min(18, length * 0.07) * self.intensity
        steps = max(3, min(32, round(length / 24)))
        self.variants = []
        self.bounds = QRectF(self.start, self.end).normalized().adjusted(-28, -28, 28, 28)
        rng = random.Random(seed)
        for _ in range(8):
            points = _zigzag(rng, self.start, self.end, amplitude, steps)
            trunk = _path(points)
            branches = []
            for i in range(1, len(points) - 1, 3):
                sign = rng.choice((-1, 1))
                tip = points[i] + delta / max(1, length) * rng.uniform(8, 22)
                tip += normal * sign * rng.uniform(9, 26) * self.intensity
                branches.append(_path(_zigzag(rng, points[i], tip, amplitude * 0.35, 3)))
            sparks = []
            for _ in range(8):
                angle = rng.uniform(0, math.tau)
                direction = QPointF(math.cos(angle), math.sin(angle))
                root = self.end + direction * rng.uniform(6, 12)
                sparks.append((root, root + direction * rng.uniform(4, 12) * self.intensity))
            self.variants.append((trunk, tuple(branches), tuple(sparks)))
            for path in (trunk, *branches):
                self.bounds = self.bounds.united(path.boundingRect().adjusted(-10, -10, 10, 10))
            for start, end in sparks:
                self.bounds = self.bounds.united(QRectF(start, end).normalized().adjusted(-3, -3, 3, 3))

    @staticmethod
    def _pen(color, alpha, width):
        return QPen(_color(color, alpha), width, Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin)

    def paint(self, painter, elapsed_ms):
        trunk, branches, sparks = self.variants[int(elapsed_ms / 65) % len(self.variants)]
        # Layer hard, narrow strokes: crimson edges enclosing a near-black core.
        for color, alpha, width in ((PURPLE, 60, 13), (CRIMSON, 55, 10),
                                    (CRIMSON, 235, 5.5), (NEON, 240, 3.5), (BLACK, 255, 1.9)):
            painter.setPen(self._pen(color, alpha, width))
            painter.drawPath(trunk)
        for color, alpha, width in ((CRIMSON, 210, 2.7), (BLACK, 255, 1)):
            painter.setPen(self._pen(color, alpha, width))
            for path in branches:
                painter.drawPath(path)
        painter.setPen(self._pen(NEON, 220, 1.3))
        for start, end in sparks:
            painter.drawLine(start, end)
        # A small fixed reticle marks the exact endpoint through the flicker.
        for color, width in ((BLACK, 4), (CRIMSON, 1.7)):
            painter.setPen(self._pen(color, 255, width))
            for dx, dy in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                corner = self.end + QPointF(dx * 7, dy * 7)
                painter.drawLine(corner, corner + QPointF(-dx * 4, 0))
                painter.drawLine(corner, corner + QPointF(0, -dy * 4))
        painter.setPen(self._pen(NEON, 255, 1.3))
        painter.drawLine(self.end - QPointF(2, 0), self.end + QPointF(2, 0))
        painter.drawLine(self.end - QPointF(0, 2), self.end + QPointF(0, 2))

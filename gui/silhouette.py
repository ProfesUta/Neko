"""Cached alpha-derived outlines for the existing SVG/embedded-PNG sprites."""

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBitmap, QImage, QPainter, QPainterPath, QRegion, QTransform


@dataclass(frozen=True)
class SpriteSilhouette:
    # Paths and anchors use normalized sprite coordinates, not screen pixels.
    path: QPainterPath
    anchors: tuple


def _area(polygon):
    points = list(polygon)
    return abs(sum(
        a.x() * b.y() - b.x() * a.y()
        for a, b in zip(points, points[1:] + points[:1])
    )) / 2


def silhouette_from_renderer(renderer, sample_size=96):
    image = QImage(sample_size, sample_size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, sample_size, sample_size))
    painter.end()
    path = QPainterPath()
    path.addRegion(QRegion(QBitmap.fromImage(image.createAlphaMask())))
    path = path.simplified()
    polygons = sorted(
        (polygon for polygon in path.toSubpathPolygons() if len(polygon) >= 3),
        key=_area, reverse=True,
    )
    if not polygons:
        # Irregular hair/body approximation for an empty or unsupported asset.
        path = QPainterPath(QPointF(48, 2))
        for x, y in ((88, 14), (68, 28), (78, 43), (62, 55), (65, 92),
                     (48, 85), (30, 92), (33, 55), (16, 43), (28, 28), (8, 14)):
            path.lineTo(x, y)
        path.closeSubpath()
        sample_size = 96
        polygons = path.toSubpathPolygons()

    # Include disconnected hands/legs; ignore isolated one-pixel texture specks.
    selected = [polygon for polygon in polygons[:10] if _area(polygon) >= 4]
    if not selected:
        selected = polygons[:1]
    outlines = []
    for polygon in selected:
        outline = QPainterPath()
        outline.addPolygon(polygon)
        outline.closeSubpath()
        outlines.append(outline)
    total_length = max(1, sum(outline.length() for outline in outlines))
    anchors = []
    for outline in outlines:
        length = outline.length()
        count = max(4, round(96 * length / total_length))
        for index in range(count):
            at = length * index / count
            point = outline.pointAtPercent(outline.percentAtLength(at))
            before = outline.pointAtPercent(outline.percentAtLength((at - 0.8) % max(1, length)))
            after = outline.pointAtPercent(outline.percentAtLength((at + 0.8) % max(1, length)))
            dx, dy = after.x() - before.x(), after.y() - before.y()
            magnitude = math.hypot(dx, dy) or 1
            normal = QPointF(-dy / magnitude, dx / magnitude)
            if path.contains(point + normal * 1.5):
                normal = -normal
            anchors.append((point / sample_size, normal))
    normalized = QTransform.fromScale(1 / sample_size, 1 / sample_size).map(path)
    return SpriteSilhouette(normalized, tuple(anchors))

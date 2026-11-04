"""Coordinate and placement math. No Qt, screen capture, or model dependency."""

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    @property
    def center(self):
        return self.x + self.width / 2, self.y + self.height / 2

    @property
    def area(self):
        return max(0, self.width) * max(0, self.height)

    def intersection(self, other):
        x, y = max(self.x, other.x), max(self.y, other.y)
        return Rect(x, y, max(0, min(self.right, other.right) - x),
                    max(0, min(self.bottom, other.bottom) - y))

    def translated(self, x, y):
        return Rect(self.x + x, self.y + y, self.width, self.height)

    def contains(self, other):
        return (self.x <= other.x and self.y <= other.y
                and self.right >= other.right and self.bottom >= other.bottom)

    def subtract(self, other):
        cut = self.intersection(other)
        if not cut.area:
            return [self]
        return [rect for rect in (
            Rect(self.x, self.y, self.width, cut.y - self.y),
            Rect(self.x, cut.bottom, self.width, self.bottom - cut.bottom),
            Rect(self.x, cut.y, cut.x - self.x, cut.height),
            Rect(cut.right, cut.y, self.right - cut.right, cut.height),
        ) if rect.area > 0]

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Monitor:
    id: str
    number: int
    name: str
    logical: Rect
    physical: Rect
    available: Rect
    scale: float = 1.0
    primary: bool = False
    capture_supported: bool = True

    def physical_to_logical(self, rect):
        return map_rect(rect, self.physical, self.logical)

    def logical_to_physical(self, rect):
        return map_rect(rect, self.logical, self.physical)

    def to_dict(self):
        return {
            "id": self.id, "number": self.number, "name": self.name,
            "logical_bounds": self.logical.to_dict(), "physical_bounds": self.physical.to_dict(),
            "available_bounds": self.available.to_dict(), "scale": self.scale,
            "primary": self.primary, "capture_supported": self.capture_supported,
        }


def map_rect(rect, source, destination):
    if not source.area or not all(math.isfinite(v) for v in asdict(rect).values()):
        raise ValueError("Invalid coordinate transform")
    sx, sy = destination.width / source.width, destination.height / source.height
    return Rect(destination.x + (rect.x - source.x) * sx,
                destination.y + (rect.y - source.y) * sy, rect.width * sx, rect.height * sy)


@dataclass(frozen=True)
class CaptureTransform:
    monitor: Monitor
    image_width: int
    image_height: int

    def image_to_physical(self, rect):
        image = Rect(0, 0, self.image_width, self.image_height)
        if not rect.area or not image.contains(rect):
            raise ValueError("Detection is outside the captured image")
        return map_rect(rect, image, self.monitor.physical)

    def image_to_logical(self, rect):
        return self.monitor.physical_to_logical(self.image_to_physical(rect))

    def to_dict(self):
        return {**self.monitor.to_dict(), "image_width": self.image_width,
                "image_height": self.image_height, "coordinate_space": "image-local pixels"}


@dataclass(frozen=True)
class SpriteMetrics:
    visible: Rect
    left_hand: tuple
    right_hand: tuple
    window_size: tuple = (330, 350)


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    direction: str
    overlap: float


def place_beside(target, available, metrics, obstacles=()):
    """Score real sprite/hand geometry around the verified target, then clamp."""
    visible = metrics.visible
    width, height = metrics.window_size
    tx, ty = target.center
    candidates = []
    for direction, hand in (("left", metrics.left_hand), ("right", metrics.right_hand)):
        side_x = target.right + 18 - visible.x if direction == "left" else target.x - 18 - visible.right
        xs = (side_x, available.x, available.right - width, tx - hand[0])
        ys = (ty - hand[1], target.y - visible.bottom - 18,
              target.bottom - visible.y + 18, available.y, available.bottom - height)
        for x in xs:
            for y in ys:
                x = round(max(available.x, min(x, max(available.x, available.right - width))))
                y = round(max(available.y, min(y, max(available.y, available.bottom - height))))
                body = visible.translated(x, y)
                hx, hy = x + hand[0], y + hand[1]
                if (direction == "left" and tx > hx) or (direction == "right" and tx < hx):
                    continue
                overlap = body.intersection(target).area
                outside = body.area - body.intersection(available).area
                occlusion = sum(body.intersection(obstacle).area for obstacle in obstacles)
                score = (overlap * 1000 + outside * 2000 + occlusion * 30
                         + abs(ty - hy) * 3 + math.hypot(tx - hx, ty - hy))
                candidates.append((score, Placement(x, y, direction, overlap)))
    if not candidates:
        return Placement(round(available.x), round(available.y), "left", target.area)
    return min(candidates, key=lambda item: item[0])[1]

"""Render the real Qt teleport renderer to PNG/GIF without opening the assistant."""

import argparse
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication

from gui.sprites import CharacterSprite
from gui.teleport import LightningBurst, TeleportSettings


def render_frame(sprite, bursts, age_ms, dpr=1):
    image = QImage(round(1000 * dpr), round(620 * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor("#0D1019"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#BDDAE8"))
    painter.setFont(QFont("Segoe UI", 12))
    painter.drawText(55, 65, "DEPARTURE")
    painter.drawText(605, 65, "ARRIVAL // CAPRI")
    rect = bursts[0].sprite_rect if age_ms < 0 else bursts[1].sprite_rect
    sprite.draw_character(painter, rect)
    for burst in bursts:
        burst.paint(painter, age_ms)
    painter.setPen(QColor("#6B8794"))
    painter.drawText(55, 580, "Instant relocation | Crimson / black silhouette lightning | 200 ms")
    painter.end()
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/teleport")
    parser.add_argument("--dpr", type=float, default=1)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    for filename in ("segoeui.ttf", "segoeuib.ttf"):
        path = Path("C:/Windows/Fonts") / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    sprite = CharacterSprite()
    sprite.setFixedSize(300, 300)
    sprite._timer.stop()
    shape = sprite.teleport_silhouette()
    settings = TeleportSettings.from_config()
    bursts = [
        LightningBurst(QRectF(55, 180, 300, 300), shape, settings, 0, False, seed=42),
        LightningBurst(QRectF(605, 180, 300, 300), shape, settings, 0, True, seed=43),
    ]
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    frames = []
    ages = [-1, *range(0, settings.duration_ms, 20), settings.duration_ms]
    for age in ages:
        frame = render_frame(sprite, bursts, age, args.dpr)
        if age in (0, 40, 100, settings.duration_ms):
            frame.save(str(output / f"frame-{age:03d}.png"))
        raw = frame.convertToFormat(QImage.Format.Format_RGBA8888)
        frames.append(Image.frombytes("RGBA", (raw.width(), raw.height()), raw.bits().tobytes()).convert("RGB"))
    frames[0].save(
        output / "teleport.gif", save_all=True, append_images=frames[1:], loop=0,
        duration=[220] + [20] * (len(frames) - 2) + [650], disposal=2,
    )
    sprite.close()
    print(f"Rendered {len(frames)} frames at DPR {args.dpr}: {output.resolve()}")


if __name__ == "__main__":
    main()

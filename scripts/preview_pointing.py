"""Render Capri's actual hand-to-target channel without capture, AI or audio."""

import argparse
import os
import time
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication

import config
from core.context import Context
from core.spatial import ActionTicket, SpatialTarget
from core.spatial_geometry import Rect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/spatial")
    parser.add_argument("--dpr", type=float, default=1)
    args = parser.parse_args()
    config.VISION_ENABLED_BY_DEFAULT = False
    from gui.window import CapriWindow
    app = QApplication.instance() or QApplication([])
    for filename in ("segoeui.ttf", "segoeuib.ttf"):
        path = Path("C:/Windows/Fonts") / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    window = CapriWindow([], Context())
    window.show()
    window._movement_timer.stop()
    window.sprite.stop_animation()
    window.context.spatial.enabled = lambda: True
    app.processEvents()
    monitor = window.monitor_manager.snapshot()[0]
    frames = []
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    try:
        for age in range(0, 520, 65):
            image = QImage(round(800 * args.dpr), round(960 * args.dpr), QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(args.dpr)
            image.fill(QColor("#0D1019"))
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for index, (x, title) in enumerate(((70, "POINT LEFT"), (570, "POINT RIGHT"))):
                box = Rect(x, 175, 160, 46)
                target = SpatialTarget(f"demo-{index}", monitor.id, "control", "Download", box,
                                       monitor.logical_to_physical(box), 1, "ocr", time.monotonic(),
                                       window.monitor_manager.revision)
                ticket = ActionTicket("show", 1, Event(), target)
                window.movement_coordinator._execute(ticket)
                assert ticket.result.get("status") == "pointing", ticket.result
                indicator = window.movement_coordinator.highlight
                indicator.now_ms = lambda: age
                painter.save()
                painter.translate(0, index * 480)
                painter.setPen(QColor("#BCD4E0"))
                painter.setFont(QFont("Segoe UI", 12))
                painter.drawText(30, 35, f"{title} // VERIFIED TARGET")
                rect = QRectF(box.x, box.y, box.width, box.height)
                painter.fillRect(rect, QColor("#1E3341"))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Download")
                window.sprite.draw_character(painter, window._sprite_screen_rect())
                indicator.paint(painter, QRectF(0, 0, 800, 480))
                painter.restore()
            painter.end()
            if age == 130:
                image.save(str(output / "lightning-target.png"))
            raw = image.convertToFormat(QImage.Format.Format_RGBA8888)
            frames.append(Image.frombytes("RGBA", (raw.width(), raw.height()), raw.bits().tobytes()).convert("RGB"))
        frames[0].save(output / "lightning-target.gif", save_all=True, append_images=frames[1:],
                       loop=0, duration=65, disposal=2)
    finally:
        window.close()
    print(f"Rendered PNG/GIF at DPR {args.dpr}: {output.resolve()}")


if __name__ == "__main__":
    main()

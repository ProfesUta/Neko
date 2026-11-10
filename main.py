import logging
import sys

from PySide6.QtWidgets import QApplication

from config import SYSTEM_PROMPT, DATA_DIR
from core.context import Context
from core.vision_diagnostics import configure_vision_logging
from gui.styles import DARK_THEME
from gui.window import CapriWindow


def build_messages():
    return [{"role": "system", "content": SYSTEM_PROMPT}], Context()


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # Local telemetry performs several requests per second. Keep real problems
    # visible without printing every successful poll (or slowing its hot path).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    configure_vision_logging(DATA_DIR)


def main():
    configure_logging()
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)
    messages, context = build_messages()
    window = CapriWindow(messages, context)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

import logging
import sys

from PySide6.QtWidgets import QApplication

from config import SYSTEM_PROMPT
from core.context import Context
from gui.styles import DARK_THEME
from gui.window import CapriWindow


def build_messages():
    return [{"role": "system", "content": SYSTEM_PROMPT}], Context()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)
    messages, context = build_messages()
    window = CapriWindow(messages, context)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

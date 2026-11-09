import os
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase

from memory import database


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE", tmp_path / "memory.db")
    # Tests must never capture the real desktop or start local model inference.
    monkeypatch.setattr("gui.window.VISION_ENABLED_BY_DEFAULT", False)
    # Existing tool/vision tests exercise the original engine; streaming and
    # League integration tests explicitly enable their routes with fake inputs.
    monkeypatch.setattr("config.CONVERSATION_STREAMING_ENABLED", False)
    monkeypatch.setattr("config.LEAGUE_LIVE_DATA_ENABLED", False)
    monkeypatch.setattr("config.VISION_LIVE_CAPTURE_ENABLED", False)
    monkeypatch.setattr("config.VOICE_PRELOAD_ON_STARTUP", False)
    monkeypatch.setattr("config.TTS_BACKEND", "kokoro")


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # Qt's offscreen backend does not discover Windows system fonts itself.
    for filename in ("segoeui.ttf", "segoeuib.ttf"):
        path = Path("C:/Windows/Fonts") / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    yield app


@pytest.fixture
def wait_until(qapp):
    def wait(predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            qapp.processEvents()
            if time.monotonic() >= deadline:
                raise AssertionError("Timed out waiting for a GUI worker")
            time.sleep(0.005)
        qapp.processEvents()
    return wait

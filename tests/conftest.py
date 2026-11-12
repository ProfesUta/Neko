import os
import time
import gc
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase

from memory import database


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    from core.inference_priority import InferencePriority
    priority = InferencePriority()
    # Real-clock cooldowns must not leak into tests with fake clocks, nor may
    # a completed test retain a foreground reservation in another test.
    for module in ("core.inference_priority", "core.model_client", "gui.workers", "gui.vision"):
        attribute = "priority" if module == "core.inference_priority" else "inference_priority"
        monkeypatch.setattr(f"{module}.{attribute}", priority)
    monkeypatch.setattr(database, "DATABASE", tmp_path / "memory.db")
    # Tests must never capture the real desktop or start local model inference.
    monkeypatch.setattr("gui.window.VISION_ENABLED_BY_DEFAULT", False)
    # Existing tool/vision tests exercise the original engine; streaming and
    # League integration tests explicitly enable their routes with fake inputs.
    monkeypatch.setattr("config.CONVERSATION_STREAMING_ENABLED", False)
    monkeypatch.setattr("config.LEAGUE_LIVE_DATA_ENABLED", False)
    monkeypatch.setattr("config.LEAGUE_CHAMP_SELECT_ENABLED", False)
    monkeypatch.setattr("config.LEAGUE_EVENTS_ENABLED", False)
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", False)
    monkeypatch.setattr("config.VISION_LIVE_CAPTURE_ENABLED", False)
    monkeypatch.setattr("config.VOICE_PRELOAD_ON_STARTUP", False)
    monkeypatch.setattr("config.CONVERSATION_PRELOAD_ON_STARTUP", False)
    monkeypatch.setattr("config.SPEAK_TYPED_REPLIES_BY_DEFAULT", False)
    monkeypatch.setattr("config.TTS_BACKEND", "kokoro")
    # Private pronunciation choices are opt-in fixtures, never personal test data.
    monkeypatch.setattr("config.PERSONAL_PRONUNCIATIONS_FILE", None)
    # Native-chunk tests opt in explicitly; legacy adapters mock complete WAVs.
    monkeypatch.setattr("config.TTS_NATIVE_STREAMING_ENABLED", False)
    monkeypatch.setattr("config.VOICE_BARGE_IN_ENABLED", False)
    yield
    from core.model_client import shutdown
    from core.latency import clear_trace
    shutdown()
    clear_trace()
    # Tests pump events manually rather than running QApplication.exec(). That
    # does not drain deferred QObject deletions automatically. Dispose them and
    # collect closed widget cycles here, on their GUI thread, before a later
    # transport/cancellation thread happens to trigger Python GC.
    from PySide6.QtCore import QEvent
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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

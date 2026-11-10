import time
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

from core.live_screen import FrameHistory, LiveScreenStream
from core.screen_access import screen_status_reply
from core.vision_diagnostics import configure_vision_logging, logger
from gui.vision import VisionWorker
from tests.test_gui import make_window
from tests.test_live_screen import frame


def connect(window, reader):
    window.vision_worker = reader
    window.context.set_screen_reader(reader)
    reader.failure.connect(window.handle_vision_failure)
    reader.recovered.connect(window.handle_vision_recovery)


def test_live_recovery_clears_capture_error_even_when_analysis_is_paused(qapp, wait_until):
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    connect(window, reader)
    allow = Event()
    @contextmanager
    def capture_factory():
        if not allow.is_set():
            raise OSError("BitBlt failed earlier")
        yield lambda: frame(2)
    stream = LiveScreenStream(capture_factory, FrameHistory(), fps=30, retry_seconds=0.05,
                              on_error=lambda message: reader.report_failure(message, "capture"),
                              on_recovered=lambda: reader.report_recovery("capture"))
    reader._live_stream = stream
    window.handle_inference_active(True)
    stream.start()
    try:
        wait_until(lambda: "BitBlt" in window.context.get_screen_status()["error"])
        allow.set()
        wait_until(lambda: stream.status()["capturing"] and not window.context.get_screen_status()["error"])
        assert reader.is_paused() and window._vision_error is None
        assert window.vision_button.text() == "VISION LIVE"
        assert "feed is running" in screen_status_reply(window.context)
        assert "error" not in screen_status_reply(window.context)
    finally:
        stream.stop()
        stream.join()
        reader._live_stream = None
        window.vision_worker = None
        window.close()


def test_capture_recovery_keeps_independent_analysis_error(qapp):
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    connect(window, reader)
    reader._live_stream = SimpleNamespace(status=lambda: {"capturing": True})
    try:
        reader.report_failure("Screen capture failed", "capture")
        reader.report_failure("Vision model is missing", "analysis")
        reader.report_recovery("capture")
        status = window.context.get_screen_status()
        assert status["errors"] == {"analysis": "Vision model is missing"}
        assert "Vision model is missing" in screen_status_reply(window.context)
        assert "tooltip" not in screen_status_reply(window.context).lower()
        assert "Vision model is missing" in window.vision_button.toolTip()
        assert window.vision_button.text() == "VISION ERROR"
    finally:
        reader._live_stream = None
        window.vision_worker = None
        window.close()


def test_question_failure_updates_spoken_reason_chat_and_tooltip(qapp, monkeypatch):
    import mss
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    connect(window, reader)
    @contextmanager
    def capture():
        yield object()
    monkeypatch.setattr(mss, "mss", capture)
    monkeypatch.setattr(reader, "_take_frame", lambda *args: frame(3))
    def analyze(*args, **kwargs):
        raise RuntimeError("The vision model timed out")
    monkeypatch.setattr(reader, "_analyze", analyze)
    try:
        window.context.refresh_screen("What's on my screen?")
        assert "timed out" in screen_status_reply(window.context)
        assert "timed out" in window.vision_button.toolTip()
        assert "timed out" in window.chat_panel.transcript.toPlainText()
        assert window.context.get_screen_status()["errors"] == {"analysis": "The vision model timed out"}
        reader.report_failure("The vision model timed out", "analysis")
        assert window.chat_panel.transcript.toPlainText().count("Analysis failed:") == 1
        monkeypatch.setattr(reader, "_analyze", lambda *args, **kwargs: ("A browser is visible", None, "A browser."))
        assert window.context.refresh_screen("What do you see?") == "A browser."
        assert not window.context.get_screen_status()["error"] and window._vision_error is None
    finally:
        window.vision_worker = None
        window.close()


def test_model_success_does_not_clear_a_capture_failure(qapp):
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    connect(window, reader)
    try:
        reader.report_failure("Desktop capture is unavailable", "capture")
        reader.report_failure("Analysis failed earlier", "analysis")
        reader.report_recovery("analysis")
        window.handle_observation("An earlier image interpretation", time.monotonic())
        window.handle_vision_status("Vision: screen understood")
        assert window.context.get_screen_status()["errors"] == {"capture": "Desktop capture is unavailable"}
        assert window._vision_error == "Desktop capture is unavailable"
    finally:
        window.vision_worker = None
        window.close()


def test_question_worker_delivers_error_to_chat_and_tooltip(qapp, wait_until, monkeypatch):
    import mss
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    connect(window, reader)
    @contextmanager
    def capture():
        yield object()
    monkeypatch.setattr(mss, "mss", capture)
    monkeypatch.setattr(reader, "_take_frame", lambda *args: frame(4))
    def fail(*args, **kwargs):
        raise RuntimeError("The local vision server rejected the request")
    monkeypatch.setattr(reader, "_analyze", fail)
    try:
        window.send_text("What's happening on my screen?")
        wait_until(lambda: window.chat_worker is None)
        assert "server rejected the request" in window.vision_button.toolTip()
        transcript = window.chat_panel.transcript.toPlainText()
        assert "Analysis failed:" in transcript
        assert "latest read failed: The local vision server rejected" in transcript
    finally:
        window.vision_worker = None
        window.close()


def test_vision_diagnostic_log_records_errors_without_response_or_image_data(tmp_path):
    previous = list(logger.handlers)
    previous_level = logger.level
    try:
        path = configure_vision_logging(tmp_path)
        worker = VisionWorker(1, "test", (100, 100), 0)
        worker.report_failure("Vision output ended before JSON was complete", "analysis")
        text = path.read_text(encoding="utf-8")
        assert "analysis failed" in text and "JSON was complete" in text
        assert len(list(tmp_path.iterdir())) == 1
    finally:
        for handler in list(logger.handlers):
            if handler not in previous:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(previous_level)


def test_successful_question_capture_clears_old_capture_error(qapp, monkeypatch):
    import mss
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    connect(window, reader)
    @contextmanager
    def capture():
        yield object()
    monkeypatch.setattr(mss, "mss", capture)
    monkeypatch.setattr(reader, "_take_frame", lambda *args: frame(1))
    monkeypatch.setattr(reader, "_analyze", lambda *args, **kwargs: ("A browser is visible", None, "Your browser is open."))
    try:
        reader.report_failure("Old display mode switch", "capture")
        assert window.context.get_screen_status()["error"]
        assert window.context.refresh_screen("What do you see?") == "Your browser is open."
        assert not window.context.get_screen_status()["error"] and window._vision_error is None
    finally:
        window.vision_worker = None
        window.close()


def test_queued_failures_and_recoveries_cannot_overwrite_newer_state(qapp):
    from threading import Thread
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    connect(window, reader)
    def updates():
        reader.report_failure("Old error", "analysis")
        reader.report_recovery("analysis")
        reader.report_failure("Current error", "analysis")
    thread = Thread(target=updates)
    try:
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        qapp.processEvents()
        assert window.context.get_screen_status()["errors"] == {"analysis": "Current error"}
        transcript = window.chat_panel.transcript.toPlainText()
        assert "Old error" not in transcript and "Current error" in transcript
    finally:
        window.vision_worker = None
        window.close()


def test_late_signals_from_replaced_reader_are_ignored(qapp):
    window = make_window(qapp)
    old, current = [VisionWorker(1, "test", (100, 100), 0) for _ in range(2)]
    connect(window, old)
    connect(window, current)
    try:
        current.report_failure("Current model error", "analysis")
        old.report_failure("Old reader failed", "capture")
        old.report_recovery("analysis")
        assert window.context.get_screen_status()["errors"] == {"analysis": "Current model error"}
        assert "Old reader failed" not in window.chat_panel.transcript.toPlainText()
    finally:
        window.vision_worker = None
        window.close()


def test_analysis_recovery_does_not_require_publishing_an_obsolete_frame(monkeypatch):
    import mss
    reader = VisionWorker(1, "test", (100, 100), 0)
    reader.interval_seconds = 0
    attempts, failures, observations = [], [], []
    @contextmanager
    def capture():
        yield object()
    def analyze(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("Temporary model error")
        return "A previous app", None, None
    def recovered(stage):
        if stage == "analysis":
            reader.stop()
    monkeypatch.setattr(mss, "mss", capture)
    monkeypatch.setattr(reader, "_take_frame", lambda *args: frame(1))
    monkeypatch.setattr(reader, "_analyze", analyze)
    monkeypatch.setattr(reader, "_live_result_current", lambda *args: False)
    reader.recovered.connect(recovered)
    reader.failure.connect(lambda stage, message: failures.append(message))
    reader.observation_ready.connect(lambda text, at: observations.append(text))
    reader.run()
    assert len(attempts) == 2 and failures == ["Temporary model error"]
    assert reader.failure_state() == {} and observations == []

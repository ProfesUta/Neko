import json
import time
from contextlib import contextmanager
from dataclasses import replace
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import Qt

from core.live_screen import CaptureLayoutChanged, FrameHistory, LiveScreenStream
from gui.vision import CapturedScreen, VisionWorker


def frame(index, at=None, source="app", region=(0, 0, 800, 600)):
    return CapturedScreen(str(index).encode(), Image.new("L", (20, 20), index % 255),
                          "League of Legends" if source == "league" else "App", source,
                          time.monotonic() if at is None else at, context_key=(region, 0))


def test_history_is_bounded_by_count_time_and_bytes_and_samples_newest():
    history = FrameHistory(max_frames=4, max_seconds=3, max_bytes=8)
    for index in range(10):
        history.put(frame(index, 100 + index))
    batch = history.sample(count=2, spacing=2, now=109)
    assert [item.jpeg for item in batch.frames] == [b"7", b"9"]
    assert history.status()["frames"] == 4
    assert not history.sample(now=111).frames
    history.put(replace(frame(10, 109.1), jpeg=b"abcdef"))
    assert history.status()["bytes"] <= 8 and history.status()["frames"] < 4
    with pytest.raises(ValueError, match="memory limit"):
        history.put(replace(frame(11, 109.2), jpeg=b"x" * 9))
    assert history.status()["frames"] == 0


def test_app_or_monitor_change_discards_previous_sequence():
    history = FrameHistory()
    history.put(frame(1, 100))
    original = history.sample(now=100)
    history.put(frame(2, 100.1, source="other"))
    other = history.sample(now=100.1)
    assert other.epoch != original.epoch and [item.jpeg for item in other.frames] == [b"2"]
    history.put(frame(3, 100.2, source="other", region=(-1920, 0, 1920, 1080)))
    moved = history.sample(now=100.2)
    assert moved.epoch != other.epoch and [item.jpeg for item in moved.frames] == [b"3"]


@pytest.mark.parametrize("failure", [OSError("BitBlt failed"), CaptureLayoutChanged()])
def test_capture_recovers_on_its_own_thread_and_closes_all_handles(failure):
    opened, closed, errors, threads = [], [], [], []
    ready = Event()
    @contextmanager
    def capture_factory():
        opened.append(len(opened))
        threads.append(get_ident())
        try:
            def read():
                threads.append(get_ident())
                if len(opened) == 1:
                    raise failure
                ready.set()
                return frame(2)
            yield read
        finally:
            threads.append(get_ident())
            closed.append(True)
    stream = LiveScreenStream(capture_factory, FrameHistory(), fps=30, retry_seconds=0.05, on_error=errors.append)
    stream.start()
    try:
        assert ready.wait(2)
    finally:
        stream.stop()
        stream.join()
    assert len(opened) == len(closed) == 2
    assert len(set(threads)) == 1 and threads[0] != get_ident()
    assert bool(errors) == isinstance(failure, OSError)
    assert not stream.status()["running"] and stream.status()["frames"] == 0


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr("config.VISION_LIVE_CAPTURE_ENABLED", True)
    monkeypatch.setattr("config.VISION_CAPTURE_FPS", 30)
    monkeypatch.setattr("config.VISION_RESUME_DELAY_SECONDS", 0)
    worker = VisionWorker(1, "test", (100, 100), 0)
    worker.interval_seconds = 0
    yield worker
    worker.stop()
    if worker.isRunning():
        assert worker.wait(3000)


def test_slow_inference_does_not_stop_capture_or_queue_old_frames(live, monkeypatch):
    captured, analyzed, streams = [], [], []
    newer, closed = Event(), Event()
    @contextmanager
    def capture_factory():
        try:
            def read():
                captured.append(len(captured))
                if len(captured) >= 6:
                    newer.set()
                return frame(len(captured))
            yield read
        finally:
            closed.set()
    def analyze(item, *args):
        analyzed.append(int(item.jpeg))
        streams.append(live._live_stream)
        if len(analyzed) == 1:
            assert newer.wait(2), "Capture stalled behind image inference"
        else:
            live.stop()
        return "Interpreted recent frames", None, None
    monkeypatch.setattr(live, "_open_live_capture", capture_factory)
    monkeypatch.setattr(live, "_analyze", analyze)
    live.run()
    assert len(analyzed) == 2 and analyzed[1] >= 6 and analyzed[1] > analyzed[0]
    assert closed.is_set() and streams[0].status()["frames"] == 0
    assert live._live_stream is None and live._telemetry_thread is None


def test_capture_continues_while_conversation_pauses_reasoning(live, monkeypatch, qapp, wait_until):
    captured, analyzed = [], []
    @contextmanager
    def capture_factory():
        def read():
            captured.append(True)
            return frame(len(captured))
        yield read
    def analyze(*args):
        analyzed.append(True)
        live.stop()
        return "Done", None, None
    monkeypatch.setattr(live, "_open_live_capture", capture_factory)
    monkeypatch.setattr(live, "_analyze", analyze)
    live.set_paused(True)
    live.start()
    wait_until(lambda: len(captured) >= 4)
    assert not analyzed and live.live_status()["capturing"]
    live.set_paused(False)
    wait_until(lambda: not live.isRunning())
    assert analyzed and live._live_stream is None


def test_changed_app_drops_old_inference_result(live, monkeypatch):
    change, captured_new = Event(), Event()
    observations, analyzed = [], []
    @contextmanager
    def capture_factory():
        def read():
            source = "new" if change.is_set() else "old"
            return frame(20, source=source)
        yield read
    def analyze(item, *args):
        analyzed.append(item.source_id)
        if item.source_id == "old":
            put = live._live_stream.history.put
            def publish(value):
                put(value)
                if value.source_id == "new":
                    captured_new.set()
            monkeypatch.setattr(live._live_stream.history, "put", publish)
            change.set()
            assert captured_new.wait(2)
        return item.source_id, None, None
    def observe(text, _at):
        observations.append(text)
        live.stop()
    monkeypatch.setattr(live, "_open_live_capture", capture_factory)
    monkeypatch.setattr(live, "_analyze", analyze)
    live.observation_ready.connect(observe)
    live.run()
    assert observations == ["new"] and analyzed == ["old", "new"]


def test_temporal_frames_have_chronological_source_metadata(monkeypatch):
    import gui.vision as module
    history = FrameHistory()
    now = time.monotonic()
    history.put(frame(1, now - 1.3))
    history.put(frame(2, now - 0.1))
    worker = VisionWorker(1, "test", (100, 100), 0)
    worker._live_stream = SimpleNamespace(history=history)
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "summary": "The health bar decreased", "visible_details": [], "urgent_advice": None})))
    monkeypatch.setattr(module, "request_chat", request)
    clip = worker._live_frame()
    worker._analyze(clip, None, Event(), False)
    message = calls[0]["messages"][-1]
    data = json.loads(message["content"].split("Context data: ", 1)[1].split("\nSchema:", 1)[0])
    assert message["images"] == [b"1", b"2"]
    assert [item["role"] for item in data["image_timeline"]] == ["earlier", "latest"]
    assert data["image_timeline"][0]["seconds_before_latest"] == pytest.approx(1.2)
    assert data["image_timeline"][1]["seconds_before_latest"] == 0
    history.clear()
    assert not worker._live_result_current(clip)
    worker._live_stream = None


def test_live_league_warning_does_not_wait_for_model(live, monkeypatch):
    monkeypatch.setattr("config.LEAGUE_LIVE_DATA_ENABLED", True)
    ready, thinking = Event(), Event()
    tips, captured = [], []
    live.set_advice_enabled(True)
    live.set_league_client(SimpleNamespace(read=lambda token: {
        "stats": {"currentHealth": 50 if thinking.is_set() else 500, "maxHealth": 1000},
        "isDead": False, "age_seconds": 0}))
    @contextmanager
    def capture_factory():
        def read():
            captured.append(True)
            return frame(len(captured), source="league")
        yield read
    def tip(item):
        tips.append(item)
        ready.set()
    live.advice_ready.connect(tip, Qt.ConnectionType.DirectConnection)
    def analyze(*args):
        thinking.set()
        assert ready.wait(2), "Telemetry was stuck behind visual inference"
        live.stop()
        return "League match", None, None
    monkeypatch.setattr(live, "_open_live_capture", capture_factory)
    monkeypatch.setattr(live, "_analyze", analyze)
    live.run()
    assert tips and tips[0].event_key == "league-low-health"
    assert len(captured) >= 4


def test_stop_before_start_never_opens_screen(live, monkeypatch):
    monkeypatch.setattr(live, "_open_live_capture", lambda: pytest.fail("VISION is off"))
    live.stop()
    live.run()
    assert live._live_stream is None


def test_stop_during_inference_closes_capture_and_clears_frames(live, monkeypatch, qapp, wait_until):
    from core.cancellation import check_cancelled
    started, closed = Event(), Event()
    @contextmanager
    def capture_factory():
        try:
            yield lambda: frame(5)
        finally:
            closed.set()
    def analyze(item, question, cancellation, allow_advice):
        started.set()
        while True:
            check_cancelled(cancellation)
            Event().wait(0.005)
    monkeypatch.setattr(live, "_open_live_capture", capture_factory)
    monkeypatch.setattr(live, "_analyze", analyze)
    live.start()
    wait_until(started.is_set)
    stream = live._live_stream
    wait_until(lambda: stream.status()["frames"] >= 4)
    live.stop()
    wait_until(lambda: not live.isRunning())
    assert closed.is_set() and stream.status()["frames"] == 0
    assert live._telemetry_thread is None and not stream.status()["running"]


def test_question_keeps_all_displays_and_labels_earlier_active_frame(monkeypatch):
    import mss
    from tests.test_multimonitor_vision import Capture, displays
    monitors = displays()
    worker = VisionWorker(1, "test", (400, 300), 0)
    worker.set_monitor_manager(monitors)
    history = FrameHistory()
    now = time.monotonic()
    for index, offset in ((1, 1.4), (2, 0.1)):
        history.put(replace(frame(index, now - offset), context_key=((0, 0, 800, 600), monitors.revision)))
    worker._live_stream = SimpleNamespace(history=history)
    monkeypatch.setattr(mss, "mss", Capture)
    frames = []
    def analyze(item, *args, **kwargs):
        frames.append(item)
        return "Changed screen", None, "The health bar decreased."
    monkeypatch.setattr(worker, "_analyze", analyze)
    worker.describe_question("What just happened in the game?")
    assert len(frames[0].images) == 3
    assert frames[0].images[0] == b"1"
    assert frames[0].timeline[0]["role"] == "earlier active display"
    assert [metadata["id"] for metadata in frames[0].monitors[1:]] == ["main", "left"]
    worker._live_stream = None


def test_topology_change_mid_capture_does_not_publish_wrong_layout(monkeypatch):
    import mss
    from tests.test_multimonitor_vision import Capture
    worker = VisionWorker(1, "test", (100, 100), 0)
    monitors = SimpleNamespace(revision=0)
    worker.set_monitor_manager(monitors)
    def take(*args):
        monitors.revision = 1
        return frame(1)
    monkeypatch.setattr(mss, "mss", Capture)
    monkeypatch.setattr(worker, "_take_frame", take)
    with worker._open_live_capture() as read:
        with pytest.raises(CaptureLayoutChanged):
            read()


def test_live_badge_and_capability_stay_on_during_conversation(qapp):
    from core.screen_access import screen_status_reply
    from tests.test_gui import make_window
    window = make_window(qapp)
    reader = VisionWorker(1, "test", (100, 100), 0)
    reader._live_stream = SimpleNamespace(status=lambda: {
        "capturing": True, "running": True, "capture_fps": 8.0, "frame_age_seconds": 0.05})
    window.vision_worker = reader
    window.context.set_screen_reader(reader)
    try:
        window.handle_inference_active(True)
        assert window.vision_button.text() == "VISION LIVE"
        assert "8.0 FPS" in window.vision_button.toolTip()
        assert "feed is running" in screen_status_reply(window.context)
    finally:
        reader._live_stream = None
        window.vision_worker = None
        window.close()

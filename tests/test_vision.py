from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from core.context import Context
from gui.vision import VisionWorker


def worker():
    return VisionWorker(15, "test", (100, 100), 2.0)


def test_static_screen_is_refreshed_before_context_expires(monkeypatch):
    vision = worker()
    image = Image.new("L", (32, 18), 100)
    monkeypatch.setattr("gui.vision.time.monotonic", lambda: 130)
    assert vision._should_analyze(image, image, 100, ImageChops, ImageStat)
    assert not vision._should_analyze(image, image, 120, ImageChops, ImageStat)


def test_scene_changes_are_compared_to_last_analyzed_frame():
    vision = worker()
    previous = Image.new("L", (32, 18), 100)
    current = Image.new("L", (32, 18), 104)
    import time
    assert vision._should_analyze(previous, current, time.monotonic(), ImageChops, ImageStat)


def test_both_companion_and_chat_are_masked():
    vision = worker()
    vision.set_capture_geometry((0, 0, 100, 100), [(10, 10, 20, 20), (50, 50, 20, 20)])
    image = Image.new("RGB", (100, 100), "white")
    capture = SimpleNamespace(grab=lambda region: SimpleNamespace(size=(100, 100), rgb=image.tobytes()))
    jpeg, _comparison = vision._capture(capture, Image, ImageDraw)
    with Image.open(BytesIO(jpeg)) as output:
        assert max(output.getpixel((20, 20))) < 40
        assert max(output.getpixel((60, 60))) < 40
        assert min(output.getpixel((85, 85))) > 230


def test_stopping_discards_inflight_observation(monkeypatch):
    import mss
    import gui.vision as module
    vision = worker()
    observations = []
    vision.observation_ready.connect(observations.append)
    class Capture:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(mss, "mss", Capture)
    monkeypatch.setattr(vision, "_capture", lambda *args: (b"image", Image.new("L", (32, 18))))
    def request(**kwargs):
        vision.stop()
        return SimpleNamespace(message=SimpleNamespace(content="late observation"))
    monkeypatch.setattr(module, "request_chat", request)
    vision.run()
    assert observations == []


def test_only_urgent_assessment_emits_advice_and_never_requests_tools(monkeypatch):
    import json
    import time
    import mss
    import gui.vision as module
    vision = worker()
    vision.set_advice_enabled(True)
    class Capture:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(mss, "mss", Capture)
    def capture(*args):
        vision._last_capture_at = time.monotonic()
        return b"image", Image.new("L", (64, 36))
    monkeypatch.setattr(vision, "_capture", capture)
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "summary": "Critical low health", "activity": "game", "visible_details": ["HP: 8"],
            "question_answer": "", "uncertainty": "", "sensitive": False,
            "urgent_advice": {"needed_now": True, "confidence": "high", "event_key": "health",
                              "evidence": "HP: 8", "text": "Take cover and heal."},
        })))
    monkeypatch.setattr(module, "request_chat", request)
    advice = []
    def accept(item):
        advice.append(item)
        vision.stop()
    vision.advice_ready.connect(accept)
    vision.run()
    assert len(advice) == 1
    assert advice[0].text == "Take cover and heal."
    assert "tools" not in calls[0]


def test_question_preempts_background_model_without_parallel_inference(monkeypatch):
    import json
    import time
    from threading import Event, Thread
    import mss
    import gui.vision as module
    from core.cancellation import check_cancelled
    vision = worker()
    started = Event()
    background_cancelled = Event()
    class Capture:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(mss, "mss", Capture)
    def capture(*args):
        vision._last_capture_at = time.monotonic()
        return b"fresh image", Image.new("L", (64, 36))
    monkeypatch.setattr(vision, "_capture", capture)
    active = [0]
    max_active = [0]
    def request(cancel_event=None, **kwargs):
        active[0] += 1
        max_active[0] = max(max_active[0], active[0])
        try:
            if kwargs["options"]["num_predict"] == 700:
                started.set()
                try:
                    while True:
                        check_cancelled(cancel_event)
                        time.sleep(0.005)
                finally:
                    background_cancelled.set()
            return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "summary": "An error is visible", "activity": "coding", "visible_details": [],
                "question_answer": "The module is missing", "uncertainty": "", "sensitive": False,
                "urgent_advice": None,
            })))
        finally:
            active[0] -= 1
    monkeypatch.setattr(module, "request_chat", request)
    background = Thread(target=vision.run)
    background.start()
    try:
        assert started.wait(2)
        vision.set_paused(True)
        text, captured_at = vision.describe_question("Why did it fail?")
        assert "module is missing" in text
        assert background_cancelled.is_set()
        assert max_active == [1]
        assert time.monotonic() - captured_at < 1
    finally:
        vision.stop()
        background.join(3)
        assert not background.is_alive()


@pytest.mark.parametrize("failure_stage", ["open", "grab", "analyze"])
def test_transient_failures_retry_with_fresh_capture_handles(monkeypatch, failure_stage):
    import mss
    import gui.vision as module
    vision = worker()
    vision.interval_seconds = 0
    attempts, observations, errors, statuses = [], [], [], []

    class Capture:
        def __enter__(self):
            attempts.append(self)
            if failure_stage == "open" and len(attempts) < 3:
                raise OSError("Display is switching modes")
            return self

        def __exit__(self, *args):
            pass

    def take_frame(*args):
        if failure_stage == "grab" and len(attempts) < 3:
            raise OSError("Display is switching modes")
        return module.CapturedScreen(b"image", Image.new("L", (20, 20)), "App", "app", 1)

    def analyze(*args):
        if failure_stage == "analyze" and len(attempts) < 3:
            raise ValueError("Vision model temporarily unavailable")
        return "Recovered screen", None, None

    def status(value):
        statuses.append(value)
        if value == "Vision: screen understood":
            vision.stop()

    monkeypatch.setattr(mss, "mss", Capture)
    monkeypatch.setattr(vision, "_take_frame", take_frame)
    monkeypatch.setattr(vision, "_analyze", analyze)
    vision.observation_ready.connect(lambda text, captured_at: observations.append(text))
    vision.error.connect(errors.append)
    vision.status_changed.connect(status)
    vision.run()
    assert len(attempts) == 3
    assert len(errors) == 1  # Repeated identical failures don't spam the user/log.
    assert observations == ["Recovered screen"]
    assert statuses[-1] == "Vision: screen understood"

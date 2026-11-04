import json
import time
from io import BytesIO
from threading import Event
from types import SimpleNamespace

import ollama
import pytest
from PIL import Image, ImageDraw

from core.spatial_geometry import CaptureTransform, Monitor, Rect
from gui.vision import CapturedMonitor, VisionWorker
from tests.test_spatial import Monitors


def displays():
    return Monitors([
        Monitor("main", 1, "Main", Rect(0, 0, 800, 600), Rect(0, 0, 800, 600), Rect(0, 0, 800, 560), 1, True),
        Monitor("left", 2, "Left", Rect(-1200, 0, 800, 600), Rect(-1200, 0, 1200, 900), Rect(-1200, 0, 800, 560), 1.5),
    ])


class Capture:
    def __init__(self):
        self.regions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def grab(self, region):
        self.regions.append(region)
        image = Image.new("RGB", (region["width"], region["height"]), "white")
        return SimpleNamespace(size=image.size, rgb=image.tobytes())


def test_capture_all_displays_preserves_resizing_transforms_and_masks_each_monitor():
    vision = VisionWorker(5, "vision", (400, 300), 2)
    vision.set_monitor_manager(displays())
    vision.set_capture_geometry((0, 0, 800, 600), [(20, 20, 60, 60), (-1100, 40, 40, 40)])
    capture = Capture()
    frames = vision._capture_monitors(capture, Image, ImageDraw, Event())
    assert len(capture.regions) == len(frames) == 2
    assert capture.regions[1] == {"left": -1200, "top": 0, "width": 1200, "height": 900}
    assert frames[0].transform.image_width == frames[1].transform.image_width == 400
    assert frames[0].image.getpixel((40, 40)) == (12, 15, 20)
    assert frames[1].image.getpixel((180, 90)) == (12, 15, 20)
    assert frames[1].transform.image_to_physical(Rect(100, 100, 50, 30)) == Rect(-900, 300, 150, 90)
    assert frames[1].transform.image_to_logical(Rect(100, 100, 50, 30)) == Rect(-1000, 200, 100, 60)


def test_question_attaches_all_images_in_declared_monitor_order(monkeypatch):
    import mss
    import gui.vision as module
    vision = VisionWorker(5, "vision", (400, 300), 2)
    vision.set_monitor_manager(displays())
    capture = Capture()
    monkeypatch.setattr(mss, "mss", lambda: capture)
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        return ollama.ChatResponse(message={"role": "assistant", "content": json.dumps({
            "summary": "Browser on monitor 1, Discord on monitor 2.", "activity": "desktop",
            "visible_details": [], "question_answer": "Discord is on monitor 2.",
            "uncertainty": "", "sensitive": False, "urgent_advice": None,
            "answer_directly": True, "requires_tools": False,
        })})
    monkeypatch.setattr(module, "request_chat", request)
    vision.describe_question("What is happening on both displays?")
    message = calls[0]["messages"][-1]
    assert len(message["images"]) == 2
    context = json.loads(message["content"].split("Context data: ", 1)[1].split("\nSchema:", 1)[0])
    assert [item["id"] for item in context["monitors_in_image_order"]] == ["main", "left"]
    assert context["monitors_in_image_order"][1]["physical_bounds"]["width"] == 1200
    assert context["monitors_in_image_order"][1]["image_width"] == 400


def test_explicit_monitor_capture_does_not_capture_other_monitors():
    vision = VisionWorker(5, "vision", (400, 300), 2)
    vision.set_monitor_manager(displays())
    capture = Capture()
    frames = vision._capture_monitors(capture, Image, ImageDraw, Event(), "2")
    assert len(frames) == 1
    assert frames[0].transform.monitor.id == "left"
    assert all(region["left"] < 0 for region in capture.regions)


def test_visual_model_boxes_are_ignored_without_pixel_or_accessibility_verification(monkeypatch):
    import mss
    import gui.vision as module
    vision = VisionWorker(5, "vision", (400, 300), 2)
    vision.set_monitor_manager(displays())
    monkeypatch.setattr(mss, "mss", Capture)
    monkeypatch.setattr("core.text_grounding.locate_text_pixels", lambda *args: [])
    monkeypatch.setattr(module, "request_chat", lambda **kwargs: ollama.ChatResponse(message={
        "role": "assistant", "content": json.dumps({"monitor_id": "left", "label": "Settings",
        "description": "Cog icon", "confidence": 0.97, "sensitive": False, "x": 99999, "y": -99999})}))
    hint, boxes = vision.locate_visual("red cog icon")
    assert hint["label"] == "Settings"
    assert boxes == []


def test_verified_ocr_coordinates_use_original_image_size_not_model_thumbnail(monkeypatch):
    vision = VisionWorker(5, "vision", (400, 300), 2)
    monitor = displays().resolve("left")
    frame = CapturedMonitor(b"small jpeg", Image.new("RGB", (1200, 900)), CaptureTransform(monitor, 400, 300), time.monotonic())
    monkeypatch.setattr("core.text_grounding.locate_text_pixels", lambda *args: [(Rect(150, 90, 90, 30), 0.95)])
    result = vision._ocr_detections([frame], "Download", "left", Event())
    assert result[0]["bounds"] == Rect(-1050, 90, 90, 30)
    assert monitor.physical_to_logical(result[0]["bounds"]) == Rect(-1100, 60, 60, 20)


def test_hotplug_during_analysis_discards_wrong_topology_result(monkeypatch):
    import mss
    import gui.vision as module
    vision = VisionWorker(5, "vision", (400, 300), 2)
    monitors = displays()
    vision.set_monitor_manager(monitors)
    monkeypatch.setattr(mss, "mss", Capture)
    def analyze(*args, **kwargs):
        monitors.revision += 1
        return "obsolete", None, "obsolete"
    monkeypatch.setattr(vision, "_analyze", analyze)
    with pytest.raises(ValueError, match="layout changed"):
        vision.describe_question("What is on my other monitor?")

import json
import time
from threading import Event
from types import SimpleNamespace

import ollama
import pytest
from PIL import Image

from core import chat as engine
from core.cancellation import OperationCancelled
from core.context import Context
from gui import vision as vision_module
from gui.vision import VisionWorker
from gui.workers import ChatWorker


def setup_reader(monkeypatch):
    import mss
    reader = VisionWorker(5, "vision-test", (1600, 1000), 2)
    class Capture:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(mss, "mss", Capture)
    def capture(*args):
        reader._last_capture_at = time.monotonic()
        return b"fresh-image", Image.new("L", (64, 36))
    monkeypatch.setattr(reader, "_capture", capture)
    return reader


def test_every_question_gets_a_fresh_focused_image_even_when_background_paused(monkeypatch):
    reader = setup_reader(monkeypatch)
    reader.set_paused(True)
    requests = []
    def ask(**kwargs):
        requests.append(kwargs)
        return ollama.ChatResponse(message={"role": "assistant", "content": json.dumps({
            "summary": "An error dialog is visible.", "activity": "coding",
            "visible_details": ["ModuleNotFoundError: requests"],
            "question_answer": "The interpreter is missing the requests package.",
            "uncertainty": "The active virtual environment is not visible.",
            "sensitive": False, "urgent_advice": None,
        })})
    monkeypatch.setattr(vision_module, "request_chat", ask)
    context = Context()
    context.set_screen_reader(reader)
    context.refresh_screen("Why is this failing?")
    assert "missing the requests package" in context.get_screen_observation()
    assert requests[0]["messages"][-1]["images"] == [b"fresh-image"]
    assert "Why is this failing?" in requests[0]["messages"][-1]["content"]
    assert requests[0]["format"]["type"] == "object"
    assert context.get_screen_status()["available"]


def test_screen_failure_clears_old_context_and_is_explicit_in_request(monkeypatch):
    reader = setup_reader(monkeypatch)
    def fail(**kwargs):
        raise RuntimeError("Vision model unavailable")
    monkeypatch.setattr(vision_module, "request_chat", fail)
    context = Context()
    context.set_screen_reader(reader)
    context.set_screen_observation("A stale old screen")
    context.refresh_screen("What is this error?")
    assert context.get_screen_observation() == ""
    assert context.get_screen_status()["error"] == "Vision model unavailable"
    request = engine.build_request_messages([{"role": "user", "content": "What is this error?"}], context)
    assert "Vision model unavailable" in request[0]["content"]


def test_turning_vision_off_during_question_discards_late_result(monkeypatch):
    reader = setup_reader(monkeypatch)
    context = Context()
    context.set_screen_reader(reader)
    def describe(question, cancel_event):
        context.set_screen_reader(None)
        return "A late result", time.monotonic()
    monkeypatch.setattr(reader, "describe_question", describe)
    context.refresh_screen("What do you see?")
    assert context.get_screen_observation() == ""
    assert not context.get_screen_status()["enabled"]


def test_user_stop_is_not_swallowed_as_a_vision_failure(monkeypatch):
    reader = setup_reader(monkeypatch)
    context = Context()
    context.set_screen_reader(reader)
    stop = Event()
    def describe(question, cancel_event):
        stop.set()
        raise OperationCancelled()
    monkeypatch.setattr(reader, "describe_question", describe)
    with pytest.raises(OperationCancelled):
        context.refresh_screen("What do you see?", stop)


def test_older_background_result_cannot_overwrite_newer_question_context(monkeypatch):
    context = Context()
    context.set_screen_observation("fresh answer evidence", captured_at=time.monotonic())
    context.set_screen_observation("old background summary", captured_at=time.monotonic() - 5)
    assert context.get_screen_observation() == "fresh answer evidence"


def test_chat_worker_uses_screen_before_answering_without_creating_fake_user_messages(monkeypatch):
    context = Context()
    seen = []
    class Reader:
        def describe_question(self, question, cancel_event):
            seen.append(question)
            return "Visible error: disk is full.", time.monotonic()
        def isInterruptionRequested(self):
            return False
    context.set_screen_reader(Reader())
    def request(**kwargs):
        assert "disk is full" in kwargs["messages"][1]["content"]
        return ollama.ChatResponse(message={"role": "assistant", "content": "The disk is full."})
    monkeypatch.setattr(engine, "request_chat", request)
    messages = [{"role": "system", "content": "persona"}]
    worker = ChatWorker(messages, context, "What went wrong?")
    worker.run()
    assert seen == ["What went wrong?"]
    assert [m["content"] for m in messages if m["role"] == "user"] == ["What went wrong?"]
    assert messages[-1]["content"] == "The disk is full."


def test_direct_screen_answer_bypasses_slow_chat_model(monkeypatch):
    from core.proactivity import ScreenReading
    context = Context()
    class Reader:
        def describe_question(self, question, cancel_event):
            return ScreenReading("Visible error: missing requests", time.monotonic(), "Install requests in this interpreter's environment.")
        def isInterruptionRequested(self):
            return False
    context.set_screen_reader(Reader())
    def forbidden(**kwargs):
        raise AssertionError("A complete screen answer should not call the chat model again")
    monkeypatch.setattr(engine, "request_chat", forbidden)
    messages = [{"role": "system", "content": "persona"}]
    worker = ChatWorker(messages, context, "What is wrong here?")
    worker.run()
    assert messages[-1] == {"role": "assistant", "content": "Install requests in this interpreter's environment."}
    assert len(messages) == 3

import time
from types import SimpleNamespace

import pytest

from core.context import Context
from core.conversation import conversation_messages, needs_fresh_screen
from core.proactivity import ScreenReading
from core.screen_access import screen_capability_question, screen_status_reply
from gui.workers import ChatWorker


@pytest.fixture(autouse=True)
def fast_chat(monkeypatch):
    monkeypatch.setattr("config.CONVERSATION_STREAMING_ENABLED", True)


@pytest.mark.parametrize("question", [
    "Watch my gameplay and give me tips", "What is happening?", "What should I do now?",
    "Give me helpful tips", "Any advice?", "Should I fight?", "What am I playing?",
    "What should I do?", "Analyze my gameplay", "How am I doing?",
])
def test_visual_advice_requests_use_actual_screenshot(question, monkeypatch):
    context = Context()
    captures = []
    def describe(question, cancellation):
        captures.append(question)
        return ScreenReading("The health bar is almost empty", time.monotonic(), "Your health is low; look for a safe retreat.")
    context.set_screen_reader(SimpleNamespace(describe_question=describe, isInterruptionRequested=lambda: False))
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("No second model needed"))
    worker = ChatWorker([], context, question)
    replies, errors = [], []
    worker.response_ready.connect(replies.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and len(captures) == 1
    assert replies == ["Your health is low; look for a safe retreat."]


@pytest.mark.parametrize("question", ["Explain gravity", "How can I improve at League of Legends?", "Give me tips on studying"])
def test_general_advice_does_not_require_new_screenshot(question):
    assert not needs_fresh_screen(question)


def test_capability_reads_first_image_then_uses_verified_state(monkeypatch):
    context = Context()
    captures = []
    def describe(question, cancellation):
        captures.append(question)
        return ScreenReading("A League match is visible", time.monotonic(), "I am text-only and cannot see screens.")
    context.set_screen_reader(SimpleNamespace(describe_question=describe, isInterruptionRequested=lambda: False))
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Capability is application state"))
    for question in ("Can you see my screen?", "Can you watch my gameplay?"):
        worker = ChatWorker([], context, question)
        replies, errors = [], []
        worker.response_ready.connect(replies.append)
        worker.error.connect(errors.append)
        worker.run()
        assert not errors and replies[0].startswith("Yes, I can read your screen")
    assert len(captures) == 1


def test_capability_off_does_not_capture_or_claim_sight(monkeypatch):
    context = Context()
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Do not ask a model about capabilities"))
    worker = ChatWorker([], context, "Can you see my screen?")
    replies = []
    worker.response_ready.connect(replies.append)
    worker.run()
    assert "Screen reading is off" in replies[0]


def test_read_failure_explains_real_error_without_second_model(monkeypatch):
    context = Context()
    def describe(*args):
        raise RuntimeError("Vision model unavailable")
    context.set_screen_reader(SimpleNamespace(describe_question=describe))
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("No slow fallback for a failed screen read"))
    worker = ChatWorker([], context, "What's happening on my screen?")
    replies = []
    worker.response_ready.connect(replies.append)
    worker.run()
    assert "latest read failed: Vision model unavailable" in replies[0]
    assert "can't see" not in replies[0]


def test_screen_prompt_explains_actual_capability_and_snapshot_age():
    context = Context()
    context.set_screen_reader(object())
    context.set_screen_observation("League match: red health bar", time.monotonic() - 4)
    prompt = conversation_messages([{"role": "user", "content": "Can you watch?"}], context)[0]["content"]
    assert '"enabled": true' in prompt and '"age_seconds": 4.0' in prompt
    assert "red health bar" in prompt and "Never say you are text-only" in prompt
    context.set_screen_error("Capture failed")
    assert "Capture failed" in screen_status_reply(context)
    context.set_screen_observation("Recovered visual read")
    assert screen_status_reply(context).startswith("Yes")


def test_capability_does_not_swallow_actual_screen_questions():
    assert screen_capability_question("Can you see my screen?")
    assert screen_capability_question("Can you watch my screen live?")
    assert screen_capability_question("Can you watch my gameplay continuously?")
    assert not screen_capability_question("Can you see my screen and explain the error?")
    assert not screen_capability_question("Can you see my health bar?")

import json
import time
from threading import Event
from types import SimpleNamespace

import pytest

from core.cancellation import OperationCancelled
from gui import vision as module
from gui.vision import CapturedScreen, VisionWorker


def response(content, done_reason=None):
    return SimpleNamespace(message=SimpleNamespace(content=content), done_reason=done_reason)


def valid_output():
    return json.dumps({
        "summary": "A low health warning is visible", "visible_details": ["Health: 8/100"],
        "question_answer": "Your health is low.", "answer_directly": True, "requires_tools": False,
        "sensitive": False, "urgent_advice": {
            "needed_now": True, "confidence": "high", "event_key": "low-health",
            "evidence": "Health: 8/100", "text": "Find a safe retreat.",
        },
    })


def setup():
    worker = VisionWorker(3, "vision-test", (100, 100), 2)
    frame = CapturedScreen(b"image", None, "Game", "game", time.monotonic(),
                           images=(b"earlier", b"latest"), timeline=({"role": "earlier"}, {"role": "latest"}))
    return worker, frame


@pytest.mark.parametrize("bad", [
    response('{"summary":"Partial", "urgent_advice":', "length"),
    response('{"summary":"Unfinished'),
    response('not JSON'),
    response('null'),
    response(valid_output(), "length"),
])
def test_malformed_or_truncated_output_retries_once_and_accepts_only_complete_advice(monkeypatch, bad):
    worker, frame = setup()
    worker.set_conversation([{"role": "user", "content": "Old context " * 200}])
    calls = []
    replies = iter((bad, response(valid_output(), "stop")))
    def request(**kwargs):
        calls.append(kwargs)
        return next(replies)
    monkeypatch.setattr(module, "request_chat", request)
    text, advice, answer = worker._analyze(frame, None, Event(), True)
    assert len(calls) == 2 and len(worker._recent) == 1
    assert "Health: 8/100" in text and advice.text == "Find a safe retreat."
    assert answer is None
    assert calls[1]["messages"][-1]["images"] == [b"earlier", b"latest"]
    assert calls[0]["options"]["num_predict"] < calls[1]["options"]["num_predict"] <= 2048
    assert "Complete every required JSON field" in calls[1]["messages"][-1]["content"]


def test_two_invalid_responses_fail_explicitly_without_publishing_partial_history(monkeypatch, caplog):
    worker, frame = setup()
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        return response('{"summary":"DO_NOT_LOG_PRIVATE_SCREEN_TEXT')
    monkeypatch.setattr(module, "request_chat", request)
    with pytest.raises(RuntimeError, match="Vision model 'vision-test'.*after one retry"):
        worker._analyze(frame, None, Event(), True)
    assert len(calls) == 2 and not worker._recent
    assert "DO_NOT_LOG_PRIVATE_SCREEN_TEXT" not in caplog.text


def test_cancellation_after_bad_response_prevents_retry(monkeypatch):
    worker, frame = setup()
    stop, calls = Event(), []
    def request(**kwargs):
        calls.append(kwargs)
        stop.set()
        return response('{"summary":"Incomplete')
    monkeypatch.setattr(module, "request_chat", request)
    with pytest.raises(OperationCancelled):
        worker._analyze(frame, None, stop, True)
    assert len(calls) == 1 and not worker._recent


def test_valid_partial_fixture_does_not_retry(monkeypatch):
    worker, frame = setup()
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        return response('{"summary":"A game is visible"}')
    monkeypatch.setattr(module, "request_chat", request)
    text, advice, answer = worker._analyze(frame, None, Event(), True)
    assert len(calls) == 1 and len(worker._recent) == 1
    assert "A game is visible" in text and advice is None and answer is None


def test_network_or_server_failure_does_not_retry(monkeypatch):
    worker, frame = setup()
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        raise ConnectionError("Ollama unavailable")
    monkeypatch.setattr(module, "request_chat", request)
    with pytest.raises(ConnectionError, match="Ollama unavailable"):
        worker._analyze(frame, None, Event(), True)
    assert len(calls) == 1 and not worker._recent

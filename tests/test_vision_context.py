import json
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import ollama
import pytest

import config
from core import vision_model
from core.cancellation import OperationCancelled


EXACT_OVERFLOW = {
    "error": {
        "code": 400,
        "message": "request (7114 tokens) exceeds the available context size (4096 tokens), try increasing it",
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 7114,
        "n_ctx": 4096,
    }
}


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(config, "VISION_CONTEXT_TOKENS", 8192, raising=False)
    monkeypatch.setattr(config, "VISION_MAX_CONTEXT_TOKENS", 16384, raising=False)
    monkeypatch.setattr(vision_model, "_context_cache", {})


def arguments():
    return {
        "model": "vision-test",
        "messages": [
            {"role": "system", "content": "Interpret the supplied frames."},
            {"role": "user", "content": json.dumps({
                "monitors_in_image_order": [{"id": "left", "physical_bounds": {"left": -1920}}, {"id": "main"}],
                "image_timeline": [{"role": "earlier", "seconds_before_latest": 1}, {"role": "latest"}],
            }), "images": [b"earlier", b"latest", b"secondary-monitor"]},
        ],
        "format": {"type": "object", "properties": {"summary": {"type": "string"}}},
        "options": {"temperature": 0, "num_predict": 450},
        "keep_alive": "15m",
    }


def test_configured_context_is_sent_on_first_request_without_mutating_input(monkeypatch):
    request = arguments()
    original = deepcopy(request)
    result = SimpleNamespace(message=SimpleNamespace(content='{"summary":"Visible screen"}'))
    calls = []
    def chat(cancel_event=None, **kwargs):
        calls.append(kwargs)
        return result
    monkeypatch.setattr(vision_model, "request_chat", chat)
    assert vision_model.request_vision(**request) is result
    assert len(calls) == 1 and calls[0]["options"]["num_ctx"] == 8192
    assert request == original
    assert calls[0]["options"]["temperature"] == 0 and calls[0]["options"]["num_predict"] == 450


@pytest.mark.parametrize("output_tokens,expected_context", [(450, 8192), (2048, 10240)])
def test_exact_reported_overflow_increases_context_and_preserves_all_request_data(monkeypatch, output_tokens, expected_context):
    monkeypatch.setattr(config, "VISION_CONTEXT_TOKENS", 4096)
    request = arguments()
    request["options"]["num_predict"] = output_tokens
    original, calls = deepcopy(request), []
    stop = Event()
    result = SimpleNamespace(message=SimpleNamespace(content='{"summary":"Recovered"}'))
    def chat(cancel_event=None, **kwargs):
        calls.append((cancel_event, deepcopy(kwargs)))
        if len(calls) == 1:
            raise ollama.ResponseError(json.dumps(EXACT_OVERFLOW), status_code=400)
        return result
    monkeypatch.setattr(vision_model, "request_chat", chat)
    assert vision_model.request_vision(cancel_event=stop, **request) is result
    assert len(calls) == 2 and all(token is stop for token, _call in calls)
    assert calls[0][1]["options"]["num_ctx"] == 4096
    assert calls[1][1]["options"]["num_ctx"] == expected_context
    assert request == original
    for _token, call in calls:
        assert {key: value for key, value in call.items() if key != "options"} == {
            key: value for key, value in original.items() if key != "options"}
        assert {key: value for key, value in call["options"].items() if key != "num_ctx"} == original["options"]


def test_text_only_context_error_counts_are_recognized(monkeypatch):
    calls = []
    def chat(cancel_event=None, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ollama.ResponseError(
                "request (9345 tokens) exceeds the available context size (8192 tokens), try increasing it",
                status_code=400)
        return "recovered"
    monkeypatch.setattr(vision_model, "request_chat", chat)
    assert vision_model.request_vision(**arguments()) == "recovered"
    assert len(calls) == 2 and calls[1]["options"]["num_ctx"] == 10240


@pytest.mark.parametrize("status,message", [
    (400, "invalid image input"),
    (400, "context size must be positive"),
    (500, json.dumps(EXACT_OVERFLOW)),
    (404, "vision-test is unavailable"),
])
def test_other_server_errors_propagate_without_retry(monkeypatch, status, message):
    calls = []
    error = ollama.ResponseError(message, status_code=status)
    def chat(cancel_event=None, **kwargs):
        calls.append(kwargs)
        raise error
    monkeypatch.setattr(vision_model, "request_chat", chat)
    with pytest.raises(ollama.ResponseError) as raised:
        vision_model.request_vision(**arguments())
    assert raised.value is error and len(calls) == 1


def test_prompt_larger_than_configured_max_fails_clearly_without_dropping_images(monkeypatch):
    calls = []
    payload = {"error": {**EXACT_OVERFLOW["error"], "n_prompt_tokens": 20000,
                         "message": "request (20000 tokens) exceeds the available context size (8192 tokens), try increasing it"}}
    def chat(cancel_event=None, **kwargs):
        calls.append(kwargs)
        raise ollama.ResponseError(json.dumps(payload), status_code=400)
    monkeypatch.setattr(vision_model, "request_chat", chat)
    with pytest.raises(RuntimeError) as raised:
        vision_model.request_vision(**arguments())
    assert "16384" in str(raised.value)
    assert len(calls) == 1 and len(calls[0]["messages"][-1]["images"]) == 3


def test_rounding_and_output_reservation_cannot_exceed_maximum(monkeypatch):
    monkeypatch.setattr(config, "VISION_CONTEXT_TOKENS", 4096)
    monkeypatch.setattr(config, "VISION_MAX_CONTEXT_TOKENS", 8000)
    calls = []
    def chat(cancel_event=None, **kwargs):
        calls.append(kwargs)
        raise ollama.ResponseError(json.dumps(EXACT_OVERFLOW), status_code=400)
    monkeypatch.setattr(vision_model, "request_chat", chat)
    with pytest.raises(RuntimeError):
        vision_model.request_vision(**arguments())
    assert len(calls) == 1


def test_cancellation_after_context_failure_prevents_retry(monkeypatch):
    stop, calls = Event(), []
    def chat(cancel_event=None, **kwargs):
        calls.append(kwargs)
        stop.set()
        raise ollama.ResponseError(json.dumps(EXACT_OVERFLOW), status_code=400)
    monkeypatch.setattr(vision_model, "request_chat", chat)
    with pytest.raises(OperationCancelled):
        vision_model.request_vision(cancel_event=stop, **arguments())
    assert len(calls) == 1


def test_already_cancelled_request_never_contacts_model(monkeypatch):
    stop = Event()
    stop.set()
    monkeypatch.setattr(vision_model, "request_chat", lambda **kwargs: pytest.fail("Cancelled request reached server"))
    with pytest.raises(OperationCancelled):
        vision_model.request_vision(cancel_event=stop, **arguments())


def test_a_second_context_overflow_is_not_retried_again(monkeypatch):
    monkeypatch.setattr(config, "VISION_CONTEXT_TOKENS", 4096)
    calls = []
    def chat(cancel_event=None, **kwargs):
        calls.append(kwargs)
        raise ollama.ResponseError(json.dumps(EXACT_OVERFLOW), status_code=400)
    monkeypatch.setattr(vision_model, "request_chat", chat)
    with pytest.raises((ollama.ResponseError, RuntimeError)):
        vision_model.request_vision(**arguments())
    assert len(calls) == 2


def test_successful_expansion_is_reused_for_next_frames(monkeypatch):
    monkeypatch.setattr(config, "VISION_CONTEXT_TOKENS", 4096)
    calls = []
    def chat(**kwargs):
        calls.append(kwargs["options"]["num_ctx"])
        if len(calls) == 1:
            raise ollama.ResponseError(json.dumps(EXACT_OVERFLOW), status_code=400)
        return "recovered"
    monkeypatch.setattr(vision_model, "request_chat", chat)
    vision_model.request_vision(**arguments())
    vision_model.request_vision(**arguments())
    assert calls == [4096, 8192, 8192]


@pytest.mark.parametrize("operation", ["analyze", "locate"])
def test_reading_and_spatial_vision_both_send_context_options(monkeypatch, operation):
    import time
    import mss
    from gui.vision import CapturedScreen, VisionWorker
    from tests.test_multimonitor_vision import Capture, displays
    calls = []
    def chat(**kwargs):
        calls.append(kwargs)
        content = '{"summary":"Visible desktop"}' if operation == "analyze" else '{"confidence":0}'
        return SimpleNamespace(message=SimpleNamespace(content=content))
    monkeypatch.setattr(vision_model, "request_chat", chat)
    worker = VisionWorker(3, "vision-test", (400, 300), 2)
    if operation == "analyze":
        worker._analyze(CapturedScreen(b"image", None, "App", "app", time.monotonic()), None, Event(), False)
    else:
        worker.set_monitor_manager(displays())
        monkeypatch.setattr(mss, "mss", Capture)
        monkeypatch.setattr(worker, "_ocr_detections", lambda *args: [])
        worker.locate_visual("Settings")
    assert len(calls) == 1 and calls[0]["options"]["num_ctx"] == 8192

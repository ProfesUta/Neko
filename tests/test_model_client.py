import asyncio
import time
from threading import Event, Timer

import httpx
import ollama
import pytest

from core import model_client
from core.cancellation import OperationCancelled


def transport_client(monkeypatch, handler):
    real = ollama.AsyncClient
    monkeypatch.setattr(model_client.ollama, "AsyncClient", lambda **kwargs: real(
        **kwargs, transport=httpx.MockTransport(handler),
    ))


def test_real_ollama_serialization_and_response(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "Ready."}, "done": True,
        })
    transport_client(monkeypatch, handler)
    result = model_client.request_chat(model="test", messages=[{"role": "user", "content": "hello"}])
    assert result.message.content == "Ready."
    assert str(requests[0].url) == "http://127.0.0.1:11434/api/chat"


def test_stop_cancels_a_blocked_http_request(monkeypatch):
    stop = Event()
    cancelled = Event()
    async def handler(request):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
    transport_client(monkeypatch, handler)
    timer = Timer(0.1, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(OperationCancelled):
            model_client.request_chat(cancel_event=stop, model="test", messages=[])
    finally:
        timer.cancel()
        timer.join()
    assert cancelled.is_set()
    assert time.monotonic() - started < 2


def test_timeout_cancels_request_and_gives_actionable_error(monkeypatch):
    cancelled = Event()
    async def handler(request):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
    transport_client(monkeypatch, handler)
    monkeypatch.setattr(model_client, "MODEL_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(RuntimeError, match="took too long"):
        model_client.request_chat(model="test", messages=[])
    assert cancelled.is_set()


def test_missing_model_gives_pull_command(monkeypatch):
    transport_client(monkeypatch, lambda request: httpx.Response(404, json={"error": "model missing"}))
    with pytest.raises(RuntimeError, match="ollama pull test-model"):
        model_client.request_chat(model="test-model", messages=[])

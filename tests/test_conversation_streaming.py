import asyncio
import json
import time
from threading import Event, Timer

import httpx
import ollama
import pytest

from core import conversation, model_client
from core.cancellation import OperationCancelled
from core.context import Context
from core.interaction import InteractionState
from core.streaming import StreamingReply
from gui.workers import ChatWorker


@pytest.fixture(autouse=True)
def streamed_conversation(monkeypatch):
    monkeypatch.setattr("config.CONVERSATION_STREAMING_ENABLED", True)
    monkeypatch.setattr(conversation, "conversation_model", lambda: "test-chat")


def test_installed_model_selection_avoids_large_vision_embedding_and_tiny_models():
    models = [{"name": name, "details": {"parameter_size": size}} for name, size in (
        ("qwen2.5:32b", "32.5B"), ("qwen2.5:7b", "7.6B"), ("qwen2.5:3b", "3.1B"),
        ("qwen2.5vl:3b", "3B"), ("qwen2.5:0.5b", "0.5B"), ("qwen3:embedding", "3B"))]
    assert conversation.select_installed_model(models, "fallback") == "qwen2.5:3b"
    assert conversation.select_installed_model([], "fallback") == "fallback"
    installed = [{"name": name, "details": {"parameter_size": size}} for name, size in (
        ("qwen2.5:32b", "32.5B"), ("qwen2.5vl:7b", "8.3B"),
        ("qwen3:14b", "14.8B"), ("qwen3:8b", "8.2B"))]
    assert conversation.select_installed_model(installed, "fallback") == "qwen3:8b"


@pytest.mark.parametrize("question", ["How are you?", "Explain gravity", "How do I improve at League of Legends?",
                                      "I'm playing Jinx, how should I manage the wave?"])
def test_general_conversation_skips_screenshot_and_tool_engine(question, monkeypatch):
    context = Context()
    class Reader:
        def set_paused(self, value):
            pass
        def describe_question(self, *args, **kwargs):
            pytest.fail("General conversation should not wait for a screenshot")
    context.set_screen_reader(Reader())
    requests = []
    def generate(**kwargs):
        requests.append(kwargs)
        kwargs["on_text"]("Start with the basics. ")
        kwargs["on_text"]("Then practice one thing at a time.")
        return ollama.ChatResponse(message={"role": "assistant", "content": "Start with the basics. Then practice one thing at a time."})
    monkeypatch.setattr(conversation, "request_chat", generate)
    worker = ChatWorker([], context, question)
    partials, answers, errors = [], [], []
    worker.response_partial.connect(partials.append)
    worker.response_ready.connect(answers.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors
    assert partials[0] == "Start with the basics. "
    assert len(answers) == 1 and answers[0] == partials[-1]
    assert "tools" not in requests[0]
    assert requests[0]["model"] == "test-chat"


def test_first_sentence_is_spoken_before_model_finishes_and_not_spoken_twice(monkeypatch):
    context = Context()
    spoken, errors = [], []
    first_played = Event()
    def speak(text, **kwargs):
        spoken.append(text)
        kwargs["on_playback_start"]()
        first_played.set()
    monkeypatch.setattr("voice.tts.speak", speak)
    def generate(**kwargs):
        kwargs["on_text"]("Keep the wave near your tower. ")
        assert first_played.wait(1), "Speech waited for full model completion"
        kwargs["on_text"]("Last-hit while you watch the map.")
        return ollama.ChatResponse(message={"role": "assistant", "content": "Keep the wave near your tower. Last-hit while you watch the map."})
    monkeypatch.setattr(conversation, "request_chat", generate)
    worker = ChatWorker([], context, "How can I manage a wave in League?", speak_reply=True)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors
    assert spoken == ["Keep the wave near your tower.", "Last-hit while you watch the map."]


def test_qwen3_conversation_disables_reasoning_and_bounds_output(monkeypatch):
    monkeypatch.setattr(conversation, "conversation_model", lambda: "qwen3:8b")
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        kwargs["on_text"]("Hello, chum.")
        return ollama.ChatResponse(message={"role": "assistant", "content": "Hello, chum."})
    monkeypatch.setattr(conversation, "request_chat", generate)
    messages = [{"role": "user", "content": "Hello"}]
    conversation.converse(messages, Context())
    assert calls[0]["think"] is False
    assert calls[0]["options"]["num_predict"] == 256
    assert calls[0]["keep_alive"] == "15m"
    assert messages[-1] == {"role": "assistant", "content": "Hello, chum."}


@pytest.mark.parametrize("question", ["Open Discord", "Remember I main Jinx", "I live in Tbilisi", "What's my CPU usage?"])
def test_execution_and_durable_memory_keep_tool_route(question):
    assert conversation.needs_tools(question)


def test_streamed_repeat_is_suppressed_but_explicit_repeat_works():
    interaction = InteractionState()
    answer = "Keep the wave near your tower. Watch the map before you push."
    interaction.accept_answer(answer, "Any advice?", True)
    spoken = []
    for question, expected in (("Anything else?", []), ("Say that again please", [
        "Keep the wave near your tower.", "Watch the map before you push."])):
        reply = StreamingReply(Event(), interaction, question, lambda text: None, spoken.append)
        reply.feed(answer)
        reply.finish()
        assert spoken == expected


def test_partial_transcript_becomes_one_final_message(qapp):
    from gui.chat_panel import ChatPanel
    panel = ChatPanel()
    try:
        panel.append_message("You", "Hello")
        panel.show_partial("One ")
        panel.show_partial("One complete answer.")
        panel.append_message("Capri", "One complete answer.")
        transcript = panel.transcript.toPlainText()
        assert transcript.count("Capri") == 1
        assert transcript.count("One complete answer.") == 1
        panel.append_message("You", "Next question")
        panel.show_partial("Next answer.")
        assert "One complete answer." in panel.transcript.toPlainText()
    finally:
        panel.close()


def test_real_ollama_stream_emits_content_before_final_response(monkeypatch):
    real = ollama.AsyncClient
    first = Event()
    requests = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (json.dumps({"message": {"role": "assistant", "content": "First. "}, "done": False}) + "\n").encode()
            assert first.is_set()
            yield (json.dumps({"message": {"role": "assistant", "content": "Second."}, "done": True}) + "\n").encode()
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=Body())
    monkeypatch.setattr(model_client.ollama, "AsyncClient", lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handler)))
    chunks = []
    def delta(text):
        chunks.append(text)
        first.set()
    result = model_client.request_chat(model="test", messages=[], on_text=delta)
    assert result.message.content == "First. Second."
    assert chunks == ["First. ", "Second."]
    assert requests[0]["stream"] is True


def test_stop_cancels_blocked_stream_after_partial_content(monkeypatch):
    real = ollama.AsyncClient
    stop, closed = Event(), Event()
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"message":{"role":"assistant","content":"First. "},"done":false}\n'
            await asyncio.sleep(30)
        async def aclose(self):
            closed.set()
    monkeypatch.setattr(model_client.ollama, "AsyncClient", lambda **kwargs: real(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))))
    timer = Timer(0.1, stop.set)
    timer.start()
    before = time.monotonic()
    try:
        with pytest.raises(OperationCancelled):
            model_client.request_chat(model="test", messages=[], cancel_event=stop, on_text=lambda text: None)
    finally:
        timer.cancel()
        timer.join()
    assert closed.is_set() and time.monotonic() - before < 2


def test_model_failure_stops_current_and_queued_speech(monkeypatch):
    from core.cancellation import check_cancelled
    started, ended = Event(), Event()
    def speak(text, cancel_event, **kwargs):
        started.set()
        try:
            while True:
                check_cancelled(cancel_event)
                time.sleep(0.005)
        finally:
            ended.set()
    monkeypatch.setattr("voice.tts.speak", speak)
    def generate(**kwargs):
        kwargs["on_text"]("This is the first sentence. ")
        assert started.wait(1)
        raise RuntimeError("Model disconnected")
    monkeypatch.setattr(conversation, "request_chat", generate)
    worker = ChatWorker([], Context(), "Explain gravity", speak_reply=True)
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert ended.is_set()
    assert errors == ["Model disconnected"]


def test_explicit_screen_question_still_takes_fresh_image(monkeypatch):
    from core.proactivity import ScreenReading
    context = Context()
    seen = []
    class Reader:
        def describe_question(self, question, cancellation):
            seen.append(question)
            return ScreenReading("Missing file dialog visible", time.monotonic(), "That file is missing.")
        def isInterruptionRequested(self):
            return False
    context.set_screen_reader(Reader())
    monkeypatch.setattr(conversation, "request_chat", lambda **kwargs: pytest.fail("Unneeded second model call"))
    worker = ChatWorker([], context, "What is that error on my screen?")
    answers = []
    worker.response_ready.connect(answers.append)
    worker.run()
    assert len(seen) == 1 and answers == ["That file is missing."]

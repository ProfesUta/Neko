from copy import deepcopy
from threading import Event

import ollama
import pytest

from core import chat as engine
from core.cancellation import OperationCancelled
from core.context import Context
from memory import database


def reply(content="", calls=()):
    return ollama.ChatResponse(message={
        "role": "assistant", "content": content,
        "tool_calls": [
            {"function": {"name": name, "arguments": arguments}}
            for name, arguments in calls
        ],
    })


def history(text="Open the editor and list the project"):
    return [{"role": "system", "content": "You are Capri."},
            {"role": "user", "content": text}]


def mock_model(monkeypatch, *responses):
    requests = []
    iterator = iter(responses)
    def request(**kwargs):
        requests.append({
            **kwargs,
            "messages": deepcopy(kwargs["messages"]),
        })
        return next(iterator)
    monkeypatch.setattr(engine, "request_chat", request)
    return requests


def test_multiple_tool_rounds_refresh_context(monkeypatch):
    context = Context()
    messages = history()
    monkeypatch.setattr(engine, "AVAILABLE_FUNCTIONS", {
        "open_application": lambda name: f"Opened application: {name}",
        "list_files": lambda folder: "Contents of: F:/Neko\n[File] main.py",
    })
    requests = mock_model(
        monkeypatch,
        reply(calls=[("open_application", {"name": "notepad"})]),
        reply(calls=[("list_files", {"folder": "F:/Neko"})]),
        reply("The editor is open, and main.py is in the project."),
    )
    answer = engine.chat(messages, context, speak_response=False)
    assert len(requests) == 3
    assert '"last_application": "notepad"' in requests[1]["messages"][1]["content"]
    assert '"last_folder": "F:/Neko"' in requests[2]["messages"][1]["content"]
    assert answer == messages[-1]["content"]
    assert len([m for m in messages if m["role"] == "system"]) == 1


def test_new_memory_is_available_in_same_turn(monkeypatch):
    seen = []
    def detect(text, cancel_event=None):
        seen.append(text)
        return "The user's name is Ada."
    monkeypatch.setattr(engine, "detect_memory", detect)
    requests = mock_model(monkeypatch, reply("Got it, Ada."))
    engine.chat(history("My name is Ada"), Context(), speak_response=False)
    assert seen == ["My name is Ada"]
    assert "The user's name is Ada." in requests[0]["messages"][1]["content"]
    assert database.get_memories() == ["The user's name is Ada."]


def test_unknown_and_failed_tools_return_results_and_recover(monkeypatch):
    def broken(name: str):
        raise OSError("Application unavailable")
    monkeypatch.setattr(engine, "AVAILABLE_FUNCTIONS", {"open_application": broken})
    requests = mock_model(
        monkeypatch,
        reply(calls=[("missing", {}), ("open_application", {"name": "paint"})]),
        reply("The application couldn't be opened."),
    )
    context = Context()
    messages = history()
    assert "couldn't" in engine.chat(messages, context, speak_response=False)
    results = [m["content"] for m in requests[1]["messages"] if m["role"] == "tool"]
    assert "unknown tool" in results[0]
    assert "Application unavailable" in results[1]
    assert context.last_application is None


def test_invalid_arguments_do_not_reach_tools(monkeypatch):
    reached = []
    def open_app(name: str):
        reached.append(name)
        return "Opened application: paint"
    monkeypatch.setattr(engine, "AVAILABLE_FUNCTIONS", {"open_application": open_app})
    assert "must be text" in engine._execute_tool("open_application", {"name": 42}, Context())
    assert "Tool error" in engine._execute_tool("open_application", {"unexpected": "x"}, Context())
    assert reached == []


def test_failed_action_does_not_replace_successful_context():
    context = Context()
    context.set_application("notepad")
    engine.update_context(context, "open_application", {"name": "paint"}, "Failed to open paint")
    assert context.last_application == "notepad"


def test_budget_handles_model_that_keeps_requesting_tools(monkeypatch):
    reached = []
    monkeypatch.setattr(engine, "MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(engine, "AVAILABLE_FUNCTIONS", {"ping": lambda: reached.append(1) or "done"})
    requests = mock_model(monkeypatch, reply(calls=[("ping", {})]), reply(calls=[("ping", {})]))
    messages = history()
    answer = engine.chat(messages, Context(), speak_response=False)
    assert len(reached) == 1
    assert requests[-1]["tools"] == []
    assert "limit" in answer
    assert len([m for m in messages if m["role"] == "tool"]) == 2
    assert "Not executed" in messages[-2]["content"]


def test_call_budget_caps_a_large_batch(monkeypatch):
    reached = []
    monkeypatch.setattr(engine, "MAX_TOOL_CALLS", 2)
    monkeypatch.setattr(engine, "AVAILABLE_FUNCTIONS", {"ping": lambda: reached.append(1) or "done"})
    requests = mock_model(
        monkeypatch, reply(calls=[("ping", {})] * 5), reply("Two calls completed."),
    )
    messages = history()
    engine.chat(messages, Context(), speak_response=False)
    assert reached == [1, 1]
    assert requests[-1]["tools"] == []
    assert len([m for m in messages if m["role"] == "tool"]) == 5


def test_stop_between_tools_prevents_later_actions_and_resolves_history(monkeypatch):
    stop = Event()
    reached = []
    def first():
        reached.append("first")
        stop.set()
        return "first done"
    monkeypatch.setattr(engine, "AVAILABLE_FUNCTIONS", {
        "first": first, "second": lambda: reached.append("second"),
    })
    mock_model(monkeypatch, reply(calls=[("first", {}), ("second", {})]))
    messages = history()
    with pytest.raises(OperationCancelled):
        engine.chat(messages, Context(), speak_response=False, cancel_event=stop)
    assert reached == ["first"]
    results = [m["content"] for m in messages if m["role"] == "tool"]
    assert results[0] == "first done"
    assert "Not executed" in results[1]
    assert messages[-1]["role"] == "assistant"


def test_live_screen_context_expires(monkeypatch):
    context = Context()
    monkeypatch.setattr("core.context.time.monotonic", lambda: 100)
    context.set_screen_observation("An editor is open.")
    assert "An editor is open." in engine.build_request_messages(history(), context)[1]["content"]
    monkeypatch.setattr("core.context.time.monotonic", lambda: 161)
    assert "An editor is open." not in engine.build_request_messages(history(), context)[1]["content"]


def test_history_trims_whole_turns(monkeypatch):
    monkeypatch.setattr(engine, "MAX_HISTORY_TURNS", 2)
    messages = [{"role": "system", "content": "persona"}]
    for i in range(4):
        messages += [
            {"role": "user", "content": str(i)},
            {"role": "assistant", "content": "tool call"},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "answer"},
        ]
    engine._trim_history(messages)
    assert messages[0]["content"] == "persona"
    assert messages[1] == {"role": "user", "content": "2"}
    assert len(messages) == 9


def test_tool_output_is_bounded_and_reported_to_ui(monkeypatch):
    monkeypatch.setattr(engine, "MAX_TOOL_RESULT_CHARS", 100)
    monkeypatch.setattr(engine, "AVAILABLE_FUNCTIONS", {"large": lambda: "x" * 1000})
    requests = mock_model(monkeypatch, reply(calls=[("large", {})]), reply("Done."))
    events = []
    engine.chat(history(), Context(), speak_response=False, tool_callback=lambda *args: events.append(args))
    assert events[0][0] == "large"
    assert events[0][1].endswith("[Result truncated.]")
    assert len(events[0][1]) < 150
    assert [m for m in requests[-1]["messages"] if m["role"] == "tool"][0]["content"] == events[0][1]

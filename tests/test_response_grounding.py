import json
import time
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image

from core.context import Context
from core.conversation import conversation_messages
from core.league import league_question
from core.proactivity import ScreenReading, direct_screen_answer, parse_assessment
from core.response_grounding import CLARIFY, UNVERIFIED, league_screen_answer
from gui.vision import CapturedScreen, VisionWorker
from gui.workers import ChatWorker


def assessment(**updates):
    data = {"summary": "League scoreboard is visible", "activity": "game",
            "visible_details": ["Jinx", "Gold: 1350"], "question_answer": "The scoreboard shows Jinx.",
            "answer_directly": True, "requires_tools": False, "uncertainty": "", "sensitive": False,
            "urgent_advice": None, "question_understood": True, "answer_verified": True,
            "answer_evidence": ["Jinx"]}
    data.update(updates)
    return data


def run_question(context, text):
    worker = ChatWorker([], context, text)
    answers, errors = [], []
    worker.response_ready.connect(answers.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and len(answers) == 1
    return answers[0]


def game():
    return {"champion": "Jinx", "currentGold": 1350, "stats": {}, "scores": {}, "age_seconds": 0}


def test_api_then_screen_then_reply_without_either_model(monkeypatch):
    monkeypatch.setattr("config.LEAGUE_LIVE_DATA_ENABLED", True)
    context = Context()
    order = []
    def api(*args):
        order.append("api")
        return game()
    def screen(*args):
        order.append("screen")
        return {"status": "checked", "interpreted": False}
    monkeypatch.setattr(context.league, "read", api)
    context.set_screen_reader(SimpleNamespace(check_screen=screen))
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Unexpected chat model"))
    monkeypatch.setattr(context, "refresh_screen", lambda *args, **kwargs: pytest.fail("API fact used vision inference"))
    assert run_question(context, "How much gold do I have?") == "You've got 1350 gold."
    assert order[0] == "api" and order[-1] == "screen" and order.count("screen") == 1


def test_api_data_does_not_skip_required_screen_reasoning(monkeypatch):
    monkeypatch.setattr("config.LEAGUE_LIVE_DATA_ENABLED", True)
    context = Context()
    order = []
    def api(*args):
        order.append("api")
        return game()
    def describe(question, cancellation, league_context=None):
        order.append("screen")
        assert league_context["api_checked"] is True
        assert league_context["own_player"]["champion"] == "Jinx"
        return ScreenReading("A League tooltip", time.monotonic(), "That tooltip shows your current buff.", grounded=True)
    monkeypatch.setattr(context.league, "read", api)
    context.set_screen_reader(SimpleNamespace(describe_question=describe, isInterruptionRequested=lambda: False))
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Second model round trip"))
    assert run_question(context, "What is this League tooltip?") == "That tooltip shows your current buff."
    assert order[0] == "api" and order[-1] == "screen"


def test_unverified_legacy_reader_cannot_bypass_league_gate(monkeypatch):
    context = Context()
    context.set_screen_reader(SimpleNamespace(
        describe_question=lambda *args, **kwargs: ScreenReading("A guess", time.monotonic(), "Invented League facts."),
        isInterruptionRequested=lambda: False))
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Unexpected fallback model"))
    assert run_question(context, "What does my League champion's Q do?") == UNVERIFIED


@pytest.mark.parametrize("updates,expected", [
    ({"question_understood": False}, CLARIFY),
    ({"question_understood": "true"}, CLARIFY),
    ({"answer_verified": False}, UNVERIFIED),
    ({"answer_verified": 1}, UNVERIFIED),
    ({"uncertainty": "The tooltip is unreadable"}, UNVERIFIED),
    ({"answer_evidence": []}, UNVERIFIED),
    ({"answer_evidence": ["Invented ability cooldown: 10 seconds"]}, UNVERIFIED),
    ({"sensitive": True}, UNVERIFIED),
    ({"requires_tools": True}, UNVERIFIED),
    ({"question_answer": ""}, UNVERIFIED),
])
def test_uncertainty_or_unobserved_evidence_blocks_claims(updates, expected):
    assert league_screen_answer(json.dumps(assessment(**updates))) == expected


def test_supported_answer_passes_the_gate():
    assert league_screen_answer(json.dumps(assessment())) == "The scoreboard shows Jinx."


def test_api_and_current_screen_go_to_one_compact_vision_call(monkeypatch):
    worker = VisionWorker(3, "test", (100, 100), 2)
    frame = CapturedScreen(b"image", Image.new("L", (10, 10)), "League of Legends", "game", time.monotonic(),
                           league_context={"api_checked": True, "own_player": game()})
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(assessment())))
    monkeypatch.setattr("gui.vision.request_chat", request)
    _, _, answer = worker._analyze(frame, "What is the champion on this League scoreboard?", Event(), False)
    assert answer == "The scoreboard shows Jinx." and len(calls) == 1
    request = calls[0]
    assert "answer_verified" in request["format"]["required"]
    assert '"api_checked": true' in request["messages"][-1]["content"]
    assert "Jinx" in request["messages"][-1]["content"]
    assert "\nSchema:" not in request["messages"][-1]["content"]  # Already sent in format.


def test_fresh_live_league_frame_is_reused_without_recapture(monkeypatch):
    import mss
    worker = VisionWorker(3, "test", (100, 100), 2)
    worker._live_stream = object()
    frame = CapturedScreen(b"current", Image.new("L", (10, 10)), "League of Legends", "game", time.monotonic(),
                           images=(b"older", b"current"))
    monkeypatch.setattr(worker, "_live_frame", lambda: frame)
    monkeypatch.setattr(worker, "_live_result_current", lambda value: True)
    monkeypatch.setattr(mss, "mss", lambda: pytest.fail("Redundant screen recapture"))
    def analyze(current, question, cancellation, allow_advice):
        assert current.images == (b"current",) and current.game["champion"] == "Jinx"
        return "Current League screen", None, UNVERIFIED
    monkeypatch.setattr(worker, "_analyze", analyze)
    reading = worker.describe_question("What does this League icon mean?", league_context={"own_player": game()})
    assert reading.grounded and reading.direct_answer == UNVERIFIED
    assert worker.check_screen()["interpreted"] is False


def test_uncertain_general_screen_read_clarifies_instead_of_guessing(monkeypatch):
    worker = VisionWorker(3, "test", (100, 100), 2)
    frame = CapturedScreen(b"pixels", None, "Desktop", "desktop", time.monotonic())
    content = assessment(uncertainty="Several icons are ambiguous", answer_verified=False)
    monkeypatch.setattr("gui.vision.request_chat", lambda **kwargs: SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(content))))
    _, _, answer = worker._analyze(frame, "What is that icon?", Event(), False)
    assert answer.startswith("I can't verify") and "specific part" in answer


@pytest.mark.parametrize("uncertainty,evidence", [("Cannot read the value", "Gold: 1350"), ("", "Unobserved warning")])
def test_unverified_proactive_notice_stays_silent(uncertainty, evidence):
    content = assessment(uncertainty=uncertainty, urgent_advice={
        "needed_now": True, "confidence": "high", "text": "Some unsupported advice.",
        "event_key": "test", "evidence": evidence})
    assert parse_assessment(json.dumps(content), "game", time.monotonic(), True)[1] is None
    if uncertainty:
        assert direct_screen_answer(json.dumps(content), "What is that?") is None


def test_proactive_chatter_is_not_passed_as_normal_dialogue():
    context = Context()
    messages = [{"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hello."},
                {"role": "assistant", "content": "A random older warning", "proactive": True},
                {"role": "user", "content": "Explain gravity"}]
    context.set_screen_observation("An unrelated old League scene")
    request = conversation_messages(messages, context)
    assert all("random older warning" not in item["content"] for item in request)
    assert "unrelated old League scene" not in request[0]["content"]


def test_a_recent_notice_is_available_only_when_user_references_it():
    context = Context()
    notice = {"role": "assistant", "content": "Your download completed.", "proactive": True,
              "captured_at": time.monotonic(), "source": "screen", "evidence": "Completed"}
    referred = conversation_messages([notice, {"role": "user", "content": "What do you mean?"}], context)
    unrelated = conversation_messages([notice, {"role": "user", "content": "Tell me a joke"}], context)
    assert "Your download completed." in referred[0]["content"]
    assert "Your download completed." not in unrelated[0]["content"]


def test_ambiguous_question_without_context_clarifies_without_model(monkeypatch):
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Unnecessary model call"))
    assert "not sure what you're referring to" in run_question(Context(), "Which one?")


@pytest.mark.parametrize("question", ["How do I build a website?", "What is my health insurance coverage?", "Explain Python levels"])
def test_generic_keywords_do_not_hijack_topics_into_league(question):
    assert not league_question(question, [{"role": "user", "content": "League of Legends"}])


def test_assistant_chatter_does_not_establish_user_league_intent():
    assert not league_question("What should I buy?", [{"role": "assistant", "content": "League lane advice"}])
    assert league_question("What should I buy?", [{"role": "user", "content": "I'm playing League"}])

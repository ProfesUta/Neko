import json
from dataclasses import replace

import pytest

from core import proactivity
from core.proactivity import AdvicePolicy, ScreenAdvice, parse_assessment


def assessment(**updates):
    data = {
        "summary": "A game is visible. The HUD shows 10 health.",
        "activity": "game", "visible_details": ["Health: 10"],
        "question_answer": "", "uncertainty": "", "sensitive": False,
        "urgent_advice": {
            "needed_now": True, "confidence": "high", "event_key": "low-health",
            "evidence": "The visible health indicator reads 10.",
            "text": "Health is low. Get to cover and heal if you can.",
        },
    }
    data.update(updates)
    return data


def test_only_high_confidence_needed_now_advice_can_be_spoken():
    text, advice = parse_assessment(json.dumps(assessment()), "Game", 100, allow_advice=True)
    assert "Health: 10" in text
    assert advice is not None
    assert advice.event_key == "low-health"
    for key, value in (("needed_now", False), ("confidence", "medium"), ("evidence", "")):
        data = assessment()
        data["urgent_advice"][key] = value
        assert parse_assessment(json.dumps(data), "Game", 100, True)[1] is None


def test_sensitive_routine_and_disabled_advice_stay_silent():
    for data, enabled in (
        (assessment(sensitive=True), True), (assessment(urgent_advice=None), True),
        (assessment(), False),
    ):
        assert parse_assessment(json.dumps(data), "Game", 100, enabled)[1] is None


def test_invalid_json_never_produces_advice():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_assessment("not json", "Game", 100, True)
    with pytest.raises(ValueError):
        parse_assessment("[]", "Game", 100, True)


def test_same_warning_is_suppressed_but_different_urgent_issue_can_follow(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(proactivity.time, "monotonic", lambda: now[0])
    policy = AdvicePolicy()
    advice = ScreenAdvice("Health low. Heal now.", "low-health", "HUD shows 10", "game", 100)
    assert policy.should_offer(advice)
    policy.record_delivery(advice)
    now[0] += 6
    assert not policy.should_offer(replace(advice, captured_at=106))
    different = replace(
        advice, text="The objective timer is almost out.", event_key="objective-timer",
        evidence="The objective timer reads 2 seconds.", captured_at=106,
    )
    assert policy.should_offer(different)


def test_old_or_recently_stopped_advice_is_not_delivered(monkeypatch):
    monkeypatch.setattr(proactivity.time, "monotonic", lambda: 150)
    advice = ScreenAdvice("Heal now", "health", "visible low health", "game", 100)
    policy = AdvicePolicy()
    assert not policy.should_offer(advice)
    fresh = replace(advice, captured_at=150)
    policy.snooze()
    assert not policy.should_offer(fresh)


def test_direct_screen_answers_do_not_replace_tool_or_memory_actions():
    from core.proactivity import direct_screen_answer
    content = json.dumps({"answer_directly": True, "requires_tools": False, "question_answer": "An explanation."})
    assert direct_screen_answer(content, "Why is this failing?") == "An explanation."
    for question in ("Please open calculator", "Can you close that app?", "Remember this preference", "Search the web for this"):
        assert direct_screen_answer(content, question) is None
    content = json.dumps({"answer_directly": True, "requires_tools": True, "question_answer": "An explanation."})
    assert direct_screen_answer(content, "What is my CPU usage?") is None

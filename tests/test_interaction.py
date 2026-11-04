from dataclasses import replace
from threading import Event

import pytest

from core import interaction
from core.context import Context
from core.interaction import InteractionState
from core.proactivity import AdvicePolicy, ScreenAdvice, direct_screen_answer
from gui.workers import VoiceWorker
from voice import tts


ANSWER = "Your health is low. Take cover and heal before fighting again."


def test_echo_filter_handles_fragments_and_minor_transcription_errors(monkeypatch):
    monkeypatch.setattr(interaction.time, "monotonic", lambda: 100)
    state = InteractionState()
    state.record_spoken(ANSWER)
    assert state.is_probable_echo("Take cover and heal before fighting again")
    assert state.is_probable_echo("Your health is low take cover and heal before fighting a gain")
    assert not state.is_probable_echo("Where is the inventory menu?")
    assert not state.is_probable_echo("Why are you saying your health is low?")
    assert not state.is_probable_echo("Yes")


def test_echo_guard_expires_and_is_shared_across_voice_workers(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interaction.time, "monotonic", lambda: now[0])
    context = Context()
    first = VoiceWorker([], context)
    second = VoiceWorker([], context)
    first.context.interaction.record_spoken(ANSWER)
    assert second.context.interaction.is_probable_echo(ANSWER)
    now[0] = 109
    assert not second.context.interaction.is_probable_echo(ANSWER)


def test_short_answers_and_explicit_repeat_requests_are_not_blocked():
    state = InteractionState()
    assert state.accept_answer("Yes", "Is it enabled?", True)
    assert state.accept_answer("Yes", "Are you sure?", True)
    assert state.accept_answer(ANSWER, "What is that warning?", True)
    assert not state.accept_answer(ANSWER, "Where is the menu?", True)
    assert state.accept_answer(ANSWER, "Repeat that please", True)
    assert not state.accept_answer(ANSWER, "Stop repeating that", True)


def test_user_speech_blocks_advice_before_any_gui_signal_is_handled():
    context = Context()
    worker = VoiceWorker([], context)
    advice = ScreenAdvice("Get to cover.", "health", "HP: 8", "game", interaction.time.monotonic())
    assert worker.offer_advice(advice)
    worker._on_speech_start()
    assert worker._pending_advice is None
    assert not worker.offer_advice(advice)
    context.interaction.finish_user_turn()
    assert not worker.offer_advice(advice)


def test_old_tip_cannot_return_after_user_turn_and_quiet_period(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interaction.time, "monotonic", lambda: now[0])
    state = InteractionState()
    now[0] = 101
    state.begin_user_turn()
    now[0] = 103
    state.finish_user_turn()
    now[0] = 112
    assert not state.allows_advice(captured_at=100)
    assert state.allows_advice(captured_at=112)


def test_advice_cannot_cancel_a_normal_reply(monkeypatch):
    context = Context()
    worker = VoiceWorker([], context)
    context.interaction.begin_user_turn()
    attempts = []
    def speak(text, cancel_event, **kwargs):
        advice = ScreenAdvice("Get to cover.", "health", "HP: 8", "game", interaction.time.monotonic())
        attempts.append(worker.offer_advice(advice))
        assert not cancel_event.is_set()
    monkeypatch.setattr(tts, "speak", speak)
    worker._speak(ANSWER)
    assert attempts == [False]


def test_failed_or_cancelled_tip_has_retry_backoff(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interaction.time, "monotonic", lambda: now[0])
    policy = AdvicePolicy()
    advice = ScreenAdvice("Get to cover and heal.", "health", "HP: 8", "game", 100)
    assert policy.should_offer(advice)
    policy.reserve(advice)
    now[0] = 106
    assert not policy.should_offer(replace(advice, captured_at=106))
    now[0] = 121
    assert policy.should_offer(replace(advice, captured_at=121))


def test_window_change_and_event_key_change_do_not_bypass_repeat_guard(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interaction.time, "monotonic", lambda: now[0])
    policy = AdvicePolicy()
    advice = ScreenAdvice("Get to cover and heal.", "low-health", "Health indicator reads 8", "window1", 100)
    policy.record_delivery(advice)
    now[0] = 106
    assert not policy.should_offer(replace(advice, source="window2", captured_at=106))
    assert not policy.should_offer(replace(
        advice, text="Use your potion right away.", event_key="critical-resource",
        evidence="Health indicator reads 7", captured_at=106,
    ))


def test_only_actual_playback_is_remembered_as_echo(monkeypatch):
    context = Context()
    worker = VoiceWorker([], context)
    def fail(*args, **kwargs):
        raise RuntimeError("No output device")
    monkeypatch.setattr(tts, "speak", fail)
    with pytest.raises(RuntimeError):
        worker._speak(ANSWER)
    assert not context.interaction.is_probable_echo(ANSWER)
    def played(*args, **kwargs):
        kwargs["on_playback_start"]()
    monkeypatch.setattr(tts, "speak", played)
    worker._speak(ANSWER)
    assert context.interaction.is_probable_echo(ANSWER)
    assert context.interaction.microphone_delay() > 0


@pytest.mark.parametrize("question", [
    "Why do you keep repeating yourself?", "Please stop repeating that",
    "Are you listening to me?", "I asked you a different question",
])
def test_conversational_complaints_do_not_get_a_generic_screen_answer(question):
    import json
    content = json.dumps({"answer_directly": True, "requires_tools": False, "question_answer": ANSWER})
    assert direct_screen_answer(content, question) is None


def test_do_not_repeat_is_not_mistaken_for_a_repeat_request():
    state = InteractionState()
    assert state.accept_answer(ANSWER, "What is that warning?", True)
    assert not state.accept_answer(ANSWER, "Don't say that again", True)
    assert not state.accept_answer(ANSWER, "Please do not tell me that again", True)
    assert state.accept_answer(ANSWER, "Please say that again", True)


def test_short_echo_is_checked_at_speech_capture_not_slow_transcription(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interaction.time, "monotonic", lambda: now[0])
    state = InteractionState()
    state.record_spoken("Okay.")
    now[0] = 110  # recognition finishes much later than capture
    assert state.is_probable_echo("Okay", captured_at=100.5)
    assert not state.is_probable_echo("Okay", captured_at=109)
    assert not state.is_probable_echo("Okay let's open the menu", captured_at=100.5)

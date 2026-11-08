import json
import time
from threading import Event
from types import SimpleNamespace

import ollama
import pytest

from core import chat as engine
from core.context import Context
from gui import vision as vision_module
from gui.vision import CapturedScreen, VisionWorker
from gui.workers import VoiceWorker
from voice import tts, voice


ANSWER = "Your health is low. Take cover and heal before fighting again."


def fake_playback(spoken):
    def speak(text, cancel_event, **kwargs):
        callback = kwargs.get("on_playback_start")
        if callback:
            callback()
        spoken.append(text)
    return speak


def run_voice(monkeypatch, transcripts, model_answer):
    context = Context()
    worker = VoiceWorker([{"role": "system", "content": "persona"}], context)
    answers = []
    inputs = []
    lines = iter(transcripts)
    monkeypatch.setattr(voice, "load_models", lambda event: None)
    monkeypatch.setattr(tts, "warm_up", lambda event: None)
    def record(*args, **kwargs):
        try:
            text = next(lines)
        except StopIteration:
            worker.stop()
            return None
        kwargs["on_speech_start"]()
        return text
    monkeypatch.setattr(voice, "record_until_silence", record)
    monkeypatch.setattr(voice, "transcribe_audio", lambda audio, **kwargs: audio)
    def request(**kwargs):
        question = [m["content"] for m in kwargs["messages"] if m["role"] == "user"][-1]
        inputs.append(question)
        return ollama.ChatResponse(message={"role": "assistant", "content": model_answer(question)})
    monkeypatch.setattr(engine, "request_chat", request)
    monkeypatch.setattr(tts, "speak", fake_playback(answers))
    worker.run()
    return worker, inputs, answers


def test_own_reply_echo_does_not_become_another_user_question(monkeypatch):
    worker, inputs, spoken = run_voice(
        monkeypatch,
        ["What does that red bar mean?", ANSWER, "Where is the settings menu?"],
        lambda question: ANSWER if "red bar" in question else "Open the pause menu and choose Settings.",
    )
    assert inputs == ["What does that red bar mean?", "Where is the settings menu?"]
    assert len(spoken) == 2


def test_identical_model_answer_is_not_spoken_over_and_over(monkeypatch):
    worker, inputs, spoken = run_voice(
        monkeypatch, ["What is that warning?", "Where is the inventory?", "How do I pause?"],
        lambda question: ANSWER,
    )
    assert len(inputs) == 3
    assert spoken == [ANSWER]


def test_explicit_repeat_request_is_still_allowed(monkeypatch):
    worker, inputs, spoken = run_voice(
        monkeypatch, ["What is that warning?", "Say that again please"], lambda question: ANSWER,
    )
    assert len(inputs) == 2
    assert spoken == [ANSWER, ANSWER]


def test_low_confidence_audio_is_not_used_as_a_question(monkeypatch):
    segment = SimpleNamespace(
        text="Thank you for watching thank you for watching", no_speech_prob=0.95,
        avg_logprob=-1.6, compression_ratio=3.5,
    )
    monkeypatch.setattr(voice, "whisper_model", SimpleNamespace(transcribe=lambda *a, **k: (iter([segment]), None)))
    assert voice.transcribe_audio(object()) == ""


def test_screen_history_does_not_recycle_an_earlier_answer(monkeypatch):
    reader = VisionWorker(5, "test", (100, 100), 2)
    requests = []
    def request(**kwargs):
        requests.append(kwargs)
        return ollama.ChatResponse(message={"role": "assistant", "content": json.dumps({
            "summary": "A game HUD is visible", "activity": "game", "visible_details": ["HP: 8"],
            "question_answer": ANSWER, "answer_directly": True, "requires_tools": False,
            "uncertainty": "", "sensitive": False, "urgent_advice": None,
        })})
    monkeypatch.setattr(vision_module, "request_chat", request)
    frame = CapturedScreen(b"synthetic", None, "Game", "game", time.monotonic())
    reader._analyze(frame, "What does that red bar mean?", Event(), False)
    reader.set_conversation([
        {"role": "user", "content": "What does that red bar mean?"},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "Where is the inventory?"},
    ])
    reader._analyze(frame, "Where is the inventory?", Event(), False)
    data_text = requests[1]["messages"][-1]["content"].split("Context data: ", 1)[1].split("\nSchema:", 1)[0]
    data = json.loads(data_text)
    assert ANSWER not in json.dumps(data["recent_screen_observations"])


def test_screen_followup_receives_the_actual_latest_dialogue(monkeypatch):
    reader = VisionWorker(5, "test", (100, 100), 2)
    conversation = [
        {"role": "system", "content": "should not be copied"},
        {"role": "user", "content": "What does that bar mean?"},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "Where is the inventory?"},
    ]
    reader.set_conversation(conversation)
    requests = []
    def request(**kwargs):
        requests.append(kwargs)
        return ollama.ChatResponse(message={"role": "assistant", "content": json.dumps({
            "summary": "Inventory key I is visible", "activity": "game", "visible_details": ["Inventory: I"],
            "question_answer": "Press I to open inventory.", "answer_directly": True,
            "requires_tools": False, "uncertainty": "", "sensitive": False, "urgent_advice": None,
        })})
    monkeypatch.setattr(vision_module, "request_chat", request)
    frame = CapturedScreen(b"synthetic", None, "Game", "game", time.monotonic())
    reader._analyze(frame, "Where is the inventory?", Event(), False)
    reader._analyze(frame, None, Event(), False)
    def data(index):
        return json.loads(requests[index]["messages"][-1]["content"].split("Context data: ", 1)[1].split("\nSchema:", 1)[0])
    assert data(0)["recent_dialogue"][-1]["content"] == "Where is the inventory?"
    assert not any(m["role"] == "system" for m in data(0)["recent_dialogue"])
    assert data(1)["recent_dialogue"] == []


def test_short_acknowledgement_echo_does_not_start_a_reply_loop(monkeypatch):
    worker, inputs, spoken = run_voice(
        monkeypatch, ["Did you understand?", "Okay.", "Where is the inventory?"],
        lambda question: "Okay." if "understand" in question else "The inventory key is I.",
    )
    assert inputs == ["Did you understand?", "Where is the inventory?"]
    assert spoken == ["Okay.", "The inventory key is I."]


def test_clear_but_soft_speech_is_not_discarded_only_for_lower_log_probability(monkeypatch):
    segment = SimpleNamespace(
        text="Where can I find the inventory menu?", no_speech_prob=0.05,
        avg_logprob=-1.1, compression_ratio=1.2,
    )
    monkeypatch.setattr(voice, "whisper_model", SimpleNamespace(transcribe=lambda *a, **k: (iter([segment]), None)))
    assert voice.transcribe_audio(object()) == "Where can I find the inventory menu?"

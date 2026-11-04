import time
from threading import Event

import ollama

from core import chat as engine
from core.cancellation import check_cancelled
from core.context import Context
from core.proactivity import ScreenAdvice
from gui.styles import DARK_THEME
from gui.vision import VisionWorker
from gui.window import CapriWindow
from gui.workers import VoiceWorker
from voice import tts, voice


def setup_window(qapp):
    qapp.setStyleSheet(DARK_THEME)
    window = CapriWindow([{"role": "system", "content": "You are Capri."}], Context())
    reader = VisionWorker(5, "fake", (100, 100), 2)
    reader.is_source_current = lambda source: True
    reader.describe_question = lambda question, cancel_event: ("Visible health is low.", time.monotonic())
    window.vision_worker = reader
    window.context.set_screen_reader(reader)
    window._sync_controls()
    return window


def candidate():
    return ScreenAdvice("Health low. Find cover and heal.", "health", "Visible HP: 8", "game", time.monotonic())


def cleanup(window, wait_until):
    window.vision_worker = None
    window.context.set_screen_reader(None)
    window.close()
    wait_until(lambda: window.advice_worker is None and window.chat_worker is None and window.voice_worker is None)


def test_advice_is_spoken_and_shown_without_activating_chat(qapp, wait_until, monkeypatch):
    spoken = []
    monkeypatch.setattr(tts, "speak", lambda text, cancel_event, **kwargs: spoken.append(text))
    window = setup_window(qapp)
    try:
        advice = candidate()
        window.handle_advice_candidate(advice)
        wait_until(lambda: window.advice_worker is None)
        assert spoken == [advice.text]
        assert window.advice_bubble.isVisible()
        assert not window.chat_panel.isVisible()
        assert advice.text in window.chat_panel.transcript.toPlainText()
        assert not [m for m in window.messages if m["role"] == "user"]
        window.handle_advice_candidate(candidate())
        assert window.advice_worker is None
        assert len(spoken) == 1
    finally:
        cleanup(window, wait_until)


def test_user_question_preempts_unsolicited_speech(qapp, wait_until, monkeypatch):
    started = Event()
    def speak(text, cancel_event, **kwargs):
        started.set()
        while True:
            check_cancelled(cancel_event)
            time.sleep(0.005)
    monkeypatch.setattr(tts, "speak", speak)
    monkeypatch.setattr(engine, "request_chat", lambda **kwargs: ollama.ChatResponse(
        message={"role": "assistant", "content": "Your health is low."},
    ))
    window = setup_window(qapp)
    try:
        window.handle_advice_candidate(candidate())
        wait_until(started.is_set)
        window.send_text("What does the warning mean?")
        wait_until(lambda: window.advice_worker is None and window.chat_worker is None and window._pending_action is None)
        assert "Your health is low." in window.chat_panel.transcript.toPlainText()
        assert [m["content"] for m in window.messages if m["role"] == "user"] == ["What does the warning mean?"]
    finally:
        cleanup(window, wait_until)


def test_turning_advice_off_cancels_queued_speech_and_keeps_screen_questions(qapp, wait_until, monkeypatch):
    def speak(text, cancel_event, **kwargs):
        while True:
            check_cancelled(cancel_event)
            time.sleep(0.005)
    monkeypatch.setattr(tts, "speak", speak)
    window = setup_window(qapp)
    try:
        window.handle_advice_candidate(candidate())
        window.toggle_advice()
        wait_until(lambda: window.advice_worker is None)
        assert not window._advice_enabled
        assert window.context.get_screen_status()["enabled"]
        assert not window.advice_bubble.isVisible()
        window.handle_advice_candidate(candidate())
        assert window.advice_worker is None
    finally:
        cleanup(window, wait_until)


def test_speech_failure_keeps_text_tip_without_opening_chat(qapp, wait_until, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("No output device")
    monkeypatch.setattr(tts, "speak", fail)
    window = setup_window(qapp)
    try:
        window.handle_advice_candidate(candidate())
        wait_until(lambda: window.advice_worker is None)
        assert window.advice_bubble.isVisible()
        assert not window.chat_panel.isVisible()
        assert "No output device" in window.chat_panel.transcript.toPlainText()
    finally:
        cleanup(window, wait_until)


def test_tip_is_held_while_user_speaks(qapp, wait_until, monkeypatch):
    window = setup_window(qapp)
    try:
        window.set_status("Speech detected")
        window.handle_advice_candidate(candidate())
        assert window.advice_worker is None
        assert window._pending_advice is None
    finally:
        cleanup(window, wait_until)


def test_voice_advice_plays_before_opening_microphone_again(monkeypatch):
    context = Context()
    reader = VisionWorker(5, "fake", (100, 100), 2)
    reader.is_source_current = lambda source: True
    context.set_screen_reader(reader)
    worker = VoiceWorker([], context)
    order = []
    monkeypatch.setattr(voice, "load_models", lambda cancel_event: None)
    monkeypatch.setattr(tts, "speak", lambda text, cancel_event, **kwargs: order.append("advice"))
    def record(*args, **kwargs):
        order.append("microphone")
        worker.stop()
        return None
    monkeypatch.setattr(voice, "record_until_silence", record)
    worker.offer_advice(candidate())
    worker.run()
    assert order == ["advice", "microphone"]
    assert worker.messages == []


def test_changed_application_discards_queued_advice(monkeypatch):
    context = Context()
    reader = VisionWorker(5, "fake", (100, 100), 2)
    reader.is_source_current = lambda source: False
    context.set_screen_reader(reader)
    worker = VoiceWorker([], context)
    spoken = []
    monkeypatch.setattr(tts, "speak", lambda *args, **kwargs: spoken.append(True))
    worker.offer_advice(candidate())
    worker._deliver_pending_advice()
    assert spoken == []


def test_startup_enables_screen_reading_and_advice_with_visible_indicator(qapp, wait_until, monkeypatch):
    import gui.window as module
    monkeypatch.setattr(module, "VISION_ENABLED_BY_DEFAULT", True)
    monkeypatch.setattr(VisionWorker, "start", lambda self: None)
    window = CapriWindow([{"role": "system", "content": "persona"}], Context())
    try:
        assert window.context.get_screen_status()["enabled"]
        assert window.advice_button.isChecked()
        assert "SCREEN ON" in window.status_label.text()
        assert not window.controls_popup.isVisible()
    finally:
        cleanup(window, wait_until)


def test_fatal_screen_error_remains_visible_without_stealing_focus(qapp, wait_until):
    window = setup_window(qapp)
    try:
        window.handle_vision_error("Capture unavailable")
        window.vision_worker_finished()
        assert "SCREEN ERROR" in window.status_label.text()
        assert "Capture unavailable" in window.chat_panel.transcript.toPlainText()
        assert not window.chat_panel.isVisible()
    finally:
        cleanup(window, wait_until)

import time

import ollama

from core import chat as engine
from core.cancellation import check_cancelled
from core.context import Context
from gui.styles import DARK_THEME
from gui.vision import VisionWorker
from gui.window import CapriWindow


def make_window(qapp):
    qapp.setStyleSheet(DARK_THEME)
    return CapriWindow([{"role": "system", "content": "You are Capri."}], Context())


def test_typed_message_runs_on_worker_and_displays_reply(qapp, wait_until, monkeypatch):
    monkeypatch.setattr(engine, "request_chat", lambda **kwargs: ollama.ChatResponse(
        message={"role": "assistant", "content": "Ready for the next gig."},
    ))
    window = make_window(qapp)
    try:
        window.chat_panel.input.setText("Hello Capri")
        window.chat_panel.submit()
        assert window.chat_worker is not None
        assert not window.voice_button.isEnabled()
        assert not window.chat_panel.send_button.isEnabled()
        wait_until(lambda: window.chat_worker is None)
        transcript = window.chat_panel.transcript.toPlainText()
        assert "Hello Capri" in transcript
        assert "Ready for the next gig." in transcript
        assert window.voice_button.isEnabled()
        assert window.chat_panel.send_button.isEnabled()
    finally:
        window.close()


def test_stop_recovers_controls_and_retains_typed_draft(qapp, wait_until, monkeypatch):
    def request(cancel_event=None, **kwargs):
        while True:
            check_cancelled(cancel_event)
            time.sleep(0.005)
    monkeypatch.setattr(engine, "request_chat", request)
    window = make_window(qapp)
    try:
        window.send_text("Hello")
        window.chat_panel.input.setText("A follow-up draft")
        window.stop_current_turn()
        wait_until(lambda: window.chat_worker is None)
        assert window.chat_panel.input.text() == "A follow-up draft"
        assert window.chat_panel.send_button.isEnabled()
        assert not window.stop_button.isEnabled()
        assert "Stopped" in window.chat_panel.transcript.toPlainText()
    finally:
        window.close()


def test_listening_allows_vision_but_inference_pauses_it(qapp):
    window = make_window(qapp)
    vision = VisionWorker(15, "test", (100, 100), 2)
    window.vision_worker = vision
    try:
        window.handle_inference_active(False)
        window.set_status("Listening")
        assert not vision.is_paused()
        window.handle_inference_active(True)
        assert vision.is_paused()
        window.handle_inference_active(False)
        window.set_status("Speaking")
        assert not vision.is_paused()
    finally:
        window.vision_worker = None
        window.close()


def test_screen_error_remains_visible_during_retry_and_clears_after_success(qapp):
    window = make_window(qapp)
    window.vision_worker = VisionWorker(5, "test", (100, 100), 2)
    window.context.set_screen_reader(window.vision_worker)
    try:
        window.handle_vision_error("Capture unavailable")
        assert window.context.get_screen_status()["error"] == "Capture unavailable"
        window.handle_vision_status("Vision: watching the active app")
        window.handle_inference_active(True)
        assert window._vision_error == "Capture unavailable"
        assert "Capture unavailable" in window.vision_button.toolTip()
        assert window.vision_button.text() == "VISION ERROR"
        window.handle_observation("The browser is visible")
        window.handle_vision_status("Vision: screen understood")
        assert window._vision_error is None
        assert window.context.get_screen_status()["error"] == ""
        assert window.vision_button.text() == "VISION PAUSED"
    finally:
        window.vision_worker = None
        window.close()


def test_vision_started_during_inference_is_already_paused(qapp, monkeypatch):
    monkeypatch.setattr(VisionWorker, "start", lambda self: None)
    window = make_window(qapp)
    try:
        window._inference_busy = True
        window.start_vision()
        assert window.vision_worker.is_paused()
    finally:
        window.vision_worker = None
        window.close()


def test_speech_detection_selects_the_actual_expression(qapp):
    window = make_window(qapp)
    try:
        window.set_status("Speech detected")
        assert window.sprite.state == "speech_detected"
        assert window.sprite._frames == ("speechdetected",)
        window.set_status("Thinking")
        assert "thinking" in window.sprite._frames
        window.set_status("Speaking")
        assert window.sprite._frames == ("talking",)
        assert window.close_button.text() == "EXIT"
    finally:
        window.close()


def test_chat_and_sprite_do_not_interpret_html(qapp):
    window = make_window(qapp)
    try:
        window.handle_response('<script>alert("test")</script>')
        assert '<script>' in window.chat_panel.transcript.toPlainText()
        window.show()
        window.show_chat()
        qapp.processEvents()
        window.toggle_controls(window.mapToGlobal(window.rect().center()))
        qapp.processEvents()
        for button in (window.chat_button, window.stop_button, window.voice_button):
            assert window.controls_popup.rect().contains(
                button.mapTo(window.controls_popup, button.rect().bottomRight())
            )
    finally:
        window.close()


def test_close_cancels_worker_and_waits_for_clean_exit(qapp, wait_until, monkeypatch):
    def request(cancel_event=None, **kwargs):
        while True:
            check_cancelled(cancel_event)
            time.sleep(0.005)
    monkeypatch.setattr(engine, "request_chat", request)
    window = make_window(qapp)
    window.show()
    window.send_text("Hello")
    window.close()
    wait_until(lambda: window.chat_worker is None and not window.isVisible())
    assert window._closing


def test_text_mode_works_without_any_voice_modules(tmp_path):
    import os
    import subprocess
    import sys
    source = """
import importlib.abc
import sys
import time
class NoAudio(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy','sounddevice','torch','torchaudio','silero_vad','faster_whisper','sherpa_onnx'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoAudio())
import ollama
from PySide6.QtWidgets import QApplication
from main import build_messages
from gui.window import CapriWindow
from core import chat
chat.request_chat = lambda **kwargs: ollama.ChatResponse(message={'role':'assistant','content':'Text works.'})
from core import conversation
conversation.request_chat = chat.request_chat
conversation.conversation_model = lambda: 'test'
app = QApplication([])
window = CapriWindow(*build_messages())
window.send_text('Hello')
deadline = time.monotonic() + 5
while window.chat_worker is not None:
    app.processEvents()
    assert time.monotonic() < deadline
    time.sleep(0.005)
assert 'Text works.' in window.chat_panel.transcript.toPlainText()
window.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True,
        timeout=10, env={**os.environ, "CAPRI_DATA_DIR": str(tmp_path), "CAPRI_VISION": "0"},
    )
    assert result.returncode == 0, result.stderr

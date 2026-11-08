from threading import Event, Thread, Timer, get_ident

import ollama
import pytest

from core.cancellation import OperationCancelled, check_cancelled
from tests.test_gui import make_window
from voice import voice, tts


@pytest.fixture
def preload(monkeypatch):
    monkeypatch.setattr("config.VOICE_PRELOAD_ON_STARTUP", True)
    monkeypatch.setattr(voice, "record_until_silence", lambda *args, **kwargs: pytest.fail("Startup must not listen"))
    monkeypatch.setattr(tts, "_play_wav", lambda *args, **kwargs: pytest.fail("Startup must not play audio"))


def test_startup_warms_both_models_on_worker_but_keeps_mic_off(preload, monkeypatch, qapp, wait_until):
    calls = []
    monkeypatch.setattr(voice, "load_models", lambda token: calls.append(("recognition", get_ident())))
    monkeypatch.setattr(tts, "warm_up", lambda token: calls.append(("speech", get_ident())))
    window = make_window(qapp)
    try:
        wait_until(lambda: window.voice_warmup_worker is None)
        assert [name for name, _thread in calls] == ["recognition", "speech"]
        assert all(thread != get_ident() for _name, thread in calls)
        assert window.voice_worker is None and window._conversation_worker() is None
        assert window.voice_button.text() == "MIC OFF" and not window.voice_button.isChecked()
        assert "Voice models ready" in window.voice_button.toolTip()
        assert window.chat_panel.send_button.isEnabled()
    finally:
        window.close()


def test_text_chat_works_during_startup_preload(preload, monkeypatch, qapp, wait_until):
    started, release = Event(), Event()
    def load(token):
        started.set()
        while not release.wait(0.005):
            check_cancelled(token)
    monkeypatch.setattr(voice, "load_models", load)
    monkeypatch.setattr(tts, "warm_up", lambda token: None)
    monkeypatch.setattr("core.chat.request_chat", lambda **kwargs: ollama.ChatResponse(
        message={"role": "assistant", "content": "Text is ready."}))
    window = make_window(qapp)
    try:
        wait_until(started.is_set)
        window.send_text("Hello")
        wait_until(lambda: window.chat_worker is None)
        assert "Text is ready." in window.chat_panel.transcript.toPlainText()
        assert window.voice_warmup_worker is not None and window.voice_worker is None
    finally:
        release.set()
        wait_until(lambda: window.voice_warmup_worker is None)
        window.close()


def test_close_cancels_preload_before_speech_loading(preload, monkeypatch, qapp, wait_until):
    started = Event()
    def load(token):
        started.set()
        while True:
            check_cancelled(token)
            token.wait(0.005)
    monkeypatch.setattr(voice, "load_models", load)
    monkeypatch.setattr(tts, "warm_up", lambda token: pytest.fail("Cancelled preload must not continue"))
    window = make_window(qapp)
    window.show()
    wait_until(started.is_set)
    window.close()
    wait_until(lambda: window.voice_warmup_worker is None and not window.isVisible())
    assert window._closing and window.voice_worker is None


def test_one_unavailable_model_does_not_block_other_preload_or_text(preload, monkeypatch, qapp, wait_until):
    def unavailable(token):
        raise RuntimeError("Recognition model unavailable")
    warmed = []
    monkeypatch.setattr(voice, "load_models", unavailable)
    monkeypatch.setattr(tts, "warm_up", lambda token: warmed.append(True))
    window = make_window(qapp)
    try:
        wait_until(lambda: window.voice_warmup_worker is None)
        assert warmed == [True]
        assert "Recognition model unavailable" in window.voice_button.toolTip()
        assert window._last_error is None and window.chat_panel.send_button.isEnabled()
        assert not window.chat_panel.isVisible() and window.voice_worker is None
    finally:
        window.close()


def test_startup_preload_can_be_disabled(monkeypatch, qapp):
    monkeypatch.setattr(voice, "load_models", lambda token: pytest.fail("Preload is disabled"))
    window = make_window(qapp)
    try:
        assert window.voice_warmup_worker is None and window.voice_worker is None
    finally:
        window.close()


def test_simultaneous_preload_and_mic_activation_construct_models_once(monkeypatch):
    monkeypatch.setattr(voice, "whisper_model", None)
    monkeypatch.setattr(voice, "vad_iterator", None)
    started, release = Event(), Event()
    calls, errors = [], []
    def load_locked(token):
        calls.append(True)
        started.set()
        assert release.wait(2)
        voice.whisper_model, voice.vad_iterator = object(), object()
    monkeypatch.setattr(voice, "_load_models_locked", load_locked)
    def load():
        try:
            voice.load_models()
        except Exception as error:
            errors.append(error)
    first, second = Thread(target=load), Thread(target=load)
    first.start()
    assert started.wait(1)
    second.start()
    try:
        second.join(0.05)
        assert second.is_alive() and calls == [True]
    finally:
        release.set()
        first.join(2)
        second.join(2)
    assert not errors and calls == [True]
    assert not first.is_alive() and not second.is_alive()


def test_stop_while_waiting_for_shared_model_load_is_cancellable(monkeypatch):
    monkeypatch.setattr(voice, "whisper_model", None)
    monkeypatch.setattr(voice, "vad_iterator", None)
    monkeypatch.setattr(voice, "_load_models_locked", lambda token: pytest.fail("Still locked"))
    stop = Event()
    timer = Timer(0.05, stop.set)
    voice._model_load_lock.acquire()
    timer.start()
    try:
        with pytest.raises(OperationCancelled):
            voice.load_models(stop)
    finally:
        voice._model_load_lock.release()
        timer.cancel()
        timer.join()

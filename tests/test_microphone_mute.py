from threading import Event

import ollama

from core.cancellation import check_cancelled
from core.context import Context
from gui.workers import VoiceWorker
from tests.test_gui import make_window


def test_typed_reply_speaks_by_default_with_microphone_off(qapp, wait_until, monkeypatch):
    monkeypatch.setattr("config.SPEAK_TYPED_REPLIES_BY_DEFAULT", True)
    monkeypatch.setattr("core.chat.request_chat", lambda **kwargs: ollama.ChatResponse(
        message={"role": "assistant", "content": "I can talk while you're muted."}))
    spoken = []
    monkeypatch.setattr("voice.tts.speak", lambda text, **kwargs: spoken.append(text))
    window = make_window(qapp)
    try:
        assert window.voice_worker is None and window.voice_button.text() == "MIC OFF"
        assert window.chat_panel.speak_replies.isChecked()
        window.chat_panel.input.setText("Hello Capri")
        window.chat_panel.submit()
        wait_until(lambda: window.chat_worker is None)
        assert spoken == ["I can talk while you're muted."]
        assert window.voice_worker is None
    finally:
        window.close()


def test_typed_reply_can_still_be_explicitly_silent(qapp, wait_until, monkeypatch):
    monkeypatch.setattr("config.SPEAK_TYPED_REPLIES_BY_DEFAULT", True)
    monkeypatch.setattr("core.chat.request_chat", lambda **kwargs: ollama.ChatResponse(
        message={"role": "assistant", "content": "Text only."}))
    monkeypatch.setattr("voice.tts.speak", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Unexpected audio")))
    window = make_window(qapp)
    errors = []
    try:
        window.chat_panel.speak_replies.setChecked(False)
        window.send_text("Hello")
        window.chat_worker.error.connect(errors.append)
        wait_until(lambda: window.chat_worker is None)
        assert not errors and "Text only." in window.chat_panel.transcript.toPlainText()
    finally:
        window.close()


def mock_recognition(monkeypatch):
    monkeypatch.setattr("voice.voice.load_models", lambda *args: None)
    monkeypatch.setattr("voice.tts.warm_up", lambda *args: None)


def test_muting_during_speech_finishes_reply_without_recording_again(qapp, wait_until, monkeypatch):
    mock_recognition(monkeypatch)
    started, finish = Event(), Event()
    recordings, completed = [], []
    def record(cancellation, **kwargs):
        recordings.append(True)
        kwargs["on_speech_start"]()
        return object()
    monkeypatch.setattr("voice.voice.record_until_silence", record)
    monkeypatch.setattr("voice.voice.transcribe_audio", lambda *args, **kwargs: "Hello")
    monkeypatch.setattr(VoiceWorker, "_answer", lambda *args: "Let me finish this sentence.")
    def speak(text, cancel_event, **kwargs):
        started.set()
        while not finish.wait(.005):
            check_cancelled(cancel_event)
        check_cancelled(cancel_event)
        completed.append(text)
    monkeypatch.setattr("voice.tts.speak", speak)
    window = make_window(qapp)
    try:
        window.start_voice()
        wait_until(started.is_set)
        worker = window.voice_worker
        window.stop_voice()
        assert window.voice_button.text() == "MIC OFF"
        assert not worker._stop_event.is_set()
        assert not window._stopping and worker.isRunning()
        finish.set()
        wait_until(lambda: window.voice_worker is None)
        assert len(recordings) == 1 and completed == ["Let me finish this sentence."]
    finally:
        finish.set()
        window.close()
        wait_until(lambda: window.voice_worker is None)


def test_muting_while_listening_closes_capture_without_creating_user_turn(qapp, wait_until, monkeypatch):
    mock_recognition(monkeypatch)
    listening, released = Event(), Event()
    def record(cancellation, **kwargs):
        listening.set()
        try:
            while True:
                check_cancelled(cancellation)
                released.wait(.005)
        finally:
            released.set()
    monkeypatch.setattr("voice.voice.record_until_silence", record)
    monkeypatch.setattr("voice.voice.transcribe_audio", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Unexpected transcription")))
    worker = VoiceWorker([], Context())
    interrupted, errors = [], []
    worker.interrupted.connect(lambda: interrupted.append(True))
    worker.error.connect(errors.append)
    worker.start()
    try:
        wait_until(listening.is_set)
        worker.mute_microphone()
        wait_until(lambda: not worker.isRunning())
        assert released.is_set() and not interrupted and not errors and not worker.messages
    finally:
        worker.stop()
        worker.wait(1000)


def test_stop_still_cancels_speech_after_input_is_muted(qapp, wait_until, monkeypatch):
    mock_recognition(monkeypatch)
    started, cancelled = Event(), Event()
    monkeypatch.setattr("voice.voice.record_until_silence", lambda *args, **kwargs: object())
    monkeypatch.setattr("voice.voice.transcribe_audio", lambda *args, **kwargs: "Hello")
    monkeypatch.setattr(VoiceWorker, "_answer", lambda *args: "A long reply.")
    def speak(text, cancel_event, **kwargs):
        started.set()
        try:
            while True:
                check_cancelled(cancel_event)
                cancelled.wait(.005)
        finally:
            cancelled.set()
    monkeypatch.setattr("voice.tts.speak", speak)
    worker = VoiceWorker([], Context())
    worker.start()
    try:
        wait_until(started.is_set)
        worker.mute_microphone()
        worker.stop()
        wait_until(lambda: not worker.isRunning())
        assert cancelled.is_set()
    finally:
        worker.stop()
        worker.wait(1000)


def test_muted_input_worker_does_not_accept_comments_it_would_discard():
    import time
    from core.proactivity import ScreenAdvice
    worker = VoiceWorker([], Context())
    worker.mute_microphone()
    advice = ScreenAdvice("An immediate warning.", "warning", "Visible evidence", "app", time.monotonic())
    assert not worker.offer_advice(advice) and worker._pending_advice is None


def test_fresh_notice_after_muting_reaches_real_tts_playback_path(qapp, wait_until, monkeypatch):
    import time
    import wave
    from threading import get_ident
    from core.proactivity import ScreenAdvice
    from tests.test_advice_delivery import setup_window, cleanup
    window = setup_window(qapp)
    played, errors = [], []
    main_thread = get_ident()
    def synthesize(text, path, cancellation, **kwargs):
        check_cancelled(cancellation)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\0\0" * 240)
    def playback(path, cancellation, on_playback_start=None):
        check_cancelled(cancellation)
        with wave.open(str(path), "rb") as audio:
            assert audio.getnframes() == 240
        if on_playback_start:
            on_playback_start()
        played.append(get_ident())
    monkeypatch.setattr("voice.tts._synthesize", synthesize)
    monkeypatch.setattr("voice.tts._play_wav", playback)
    monkeypatch.setattr("voice.voice.record_until_silence", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MIC OFF opened input")))
    try:
        listener = VoiceWorker([], window.context)
        window.context.interaction.begin_user_turn()
        listener.mute_microphone()
        window.context.interaction.finish_user_turn()
        # A stale status label is not an active request and must not mute output.
        window.set_status("Transcribing")
        item = ScreenAdvice("Your download has completed.", "download-complete", "Completed download", "game", time.monotonic())
        window.handle_advice_candidate(item)
        assert window.advice_worker is not None
        window.advice_worker.advice_error.connect(errors.append)
        wait_until(lambda: window.advice_worker is None)
        assert len(played) == 1 and played[0] != main_thread and not errors
        assert window.voice_worker is None and window.voice_button.text() == "MIC OFF"
        assert not window.vision_worker.is_paused()
    finally:
        cleanup(window, wait_until)

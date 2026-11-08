import pytest

from core.quick_actions import parse_location_request
from gui.vision import VisionWorker
from gui.workers import VoiceWorker
from tests.test_spatial import gui_spatial


@pytest.mark.parametrize("text,query,monitor", [
    ("Where is Discord aplication on my screen?", "Discord", None),
    ("Capri, please show me where Discord is on my second monitor.", "Discord", "2"),
    ("Where's my browser?", "browser", None),
    ("Can you point to Microsoft Edge on monitor 2?", "Edge", "2"),
    ("Find Discord, please.", "Discord", None),
    ("Locate VS Code on my screen.", "VSCode", None),
    ("Where is the download button?", "download button", None),
    ("Show me the Recycle Bin icon on display 3.", "recycle bin icon", "3"),
])
def test_simple_location_commands_can_run_without_model_planning(text, query, monitor):
    request = parse_location_request(text)
    assert request is not None
    assert (request.query, request.monitor_id) == (query, monitor)


@pytest.mark.parametrize("text", [
    "Where is Paris?", "Why is Discord slow?", "Open Discord",
    "Where is Discord and how do I change my microphone?", "Don't show me Discord",
    "Where is the download button and is the file safe?", "Show me how to use Discord",
])
def test_complex_or_non_screen_questions_keep_the_normal_conversation_path(text):
    assert parse_location_request(text) is None


def test_app_action_and_spoken_reply_bypass_both_models_even_with_busy_vision(gui_spatial, wait_until, monkeypatch, caplog):
    import config
    import voice.tts as tts
    from core import chat as engine
    window, spatial, _backend = gui_spatial
    monkeypatch.setattr(config, "SPATIAL_POINTING_HOLD_MS", 0)
    reader = VisionWorker(5, "test", (100, 100), 2)
    # Model slot intentionally stays busy: native app location must not wait for it.
    reader._model_lock.acquire()
    window.context.set_screen_reader(reader)
    def forbidden(*args, **kwargs):
        raise AssertionError("A direct app lookup must not call chat or screen vision")
    monkeypatch.setattr(engine, "request_chat", forbidden)
    monkeypatch.setattr(reader, "describe_question", forbidden)
    monkeypatch.setattr(reader, "locate_visual", forbidden)
    spoken = []
    def speak(text, **kwargs):
        assert window.sprite.pointing_direction is not None
        assert window.movement_coordinator.highlight.beam is not None
        assert kwargs["before_playback"]() is None
        kwargs["on_playback_start"]()
        spoken.append(text)
    monkeypatch.setattr(tts, "speak", speak)
    try:
        with caplog.at_level("INFO"):
            window.send_text("Where is Discord aplication on my screen?", speak_reply=True)
            wait_until(lambda: window.chat_worker is None)
        assert len(spoken) == 1 and "monitor 1" in spoken[0]
        assert "Direct spatial action: pointing" in caplog.text
        assert window.messages[-1]["content"] == spoken[0]
    finally:
        reader._model_lock.release()
        window.context.set_screen_reader(None)


def test_missing_app_is_answered_without_slow_model_or_false_pointing(gui_spatial, wait_until, monkeypatch):
    from core import chat as engine
    window, spatial, backend = gui_spatial
    backend.items = []
    monkeypatch.setattr(engine, "request_chat", lambda **kwargs: pytest.fail("Unexpected model call"))
    window.send_text("Where is Discord?")
    wait_until(lambda: window.chat_worker is None)
    assert "can't find a visible Discord" in window.messages[-1]["content"]
    assert window.sprite.pointing_direction is None
    assert window.movement_coordinator.highlight.beam is None


def test_voice_preempts_screen_inference_at_speech_start_before_gui_handles_signals():
    from core.context import Context
    context = Context()
    reader = VisionWorker(5, "test", (100, 100), 2)
    context.set_screen_reader(reader)
    worker = VoiceWorker([], context)
    assert not reader.is_paused()
    worker._on_speech_start()
    assert reader.is_paused()


def test_voice_warms_output_before_listening(monkeypatch):
    from core.context import Context
    from voice import voice, tts
    order = []
    worker = VoiceWorker([], Context())
    monkeypatch.setattr(voice, "load_models", lambda *args: order.append("recognition"))
    monkeypatch.setattr(tts, "warm_up", lambda *args: order.append("speech output"))
    def record(*args, **kwargs):
        order.append("listen")
        worker.stop()
    monkeypatch.setattr(voice, "record_until_silence", record)
    worker.run()
    assert order == ["recognition", "speech output", "listen"]

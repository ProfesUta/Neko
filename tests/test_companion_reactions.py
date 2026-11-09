import pytest

from core.companion_style import reply_delivery
from core.context import Context
from gui.sprites import CharacterSprite
from gui.workers import ChatWorker
from tests.test_gui import make_window


@pytest.mark.parametrize("question,answer,urgent,style,expression", [
    ("Thanks, that worked!", "There we go. You did the hard part.", False, "happy", "happy"),
    ("I finally won that League match!", "There it is. Victory suits you.", False, "happy", "happy"),
    ("I'm frustrated. Please be serious.", "Let's take it one step at a time.", False, "warm", None),
    ("Tell me a joke", "Your loading bar has more ambition than your router.", False, "playful", "happy"),
    ("Thanks", "The vision model failed to load.", False, "focused", None),
    ("Explain this code", "The loop visits each item once.", False, "focused", None),
    ("Thanks", "Your health is critically low.", True, "urgent", None),
])
def test_delivery_takes_distress_failure_and_urgency_into_account(question, answer, urgent, style, expression):
    delivery = reply_delivery(question, answer, urgent)
    assert delivery.style == style and delivery.expression == expression


def test_reaction_during_speech_restores_talking_and_preserves_pointing(qapp, wait_until):
    sprite = CharacterSprite()
    try:
        sprite.set_activity("speaking")
        target = sprite.render_target()
        assert sprite.react("happy", 20)
        assert sprite.state == "speaking" and sprite.expression == "happy"
        assert sprite.render_target() == target
        wait_until(lambda: sprite.expression is None)
        assert sprite._frames == ("talking",) and sprite.state == "speaking"
        sprite.start_pointing("left")
        assert not sprite.react("happy")
        assert sprite._frames == ("pointingleft",)
        sprite.end_pointing()
        sprite.set_activity("listening")
        assert not sprite.react("happy") and sprite._frames == ("listening",)
    finally:
        sprite.close()


def test_expression_is_emitted_when_audio_starts_not_during_synthesis(monkeypatch):
    worker = ChatWorker([], Context(), "Thanks, that worked!", speak_reply=True)
    worker._question = worker.text
    reactions, styles = [], []
    worker.reaction_ready.connect(reactions.append)
    def speak(text, **kwargs):
        assert not reactions
        styles.append(kwargs["style"])
        kwargs["on_playback_start"]()
        assert reactions == ["happy"]
    monkeypatch.setattr("voice.tts.speak", speak)
    worker._speak("There we go. You did the hard part.")
    assert styles == ["happy"]


def test_only_current_worker_can_react_and_stop_clears_pose(qapp):
    window = make_window(qapp)
    current = ChatWorker(window.messages, window.context, "Thanks", True)
    previous = ChatWorker(window.messages, window.context, "Hi", True)
    window.chat_worker = current
    window._connect_worker(current)
    previous.reaction_ready.connect(window.handle_reply_reaction)
    try:
        window.set_status("Speaking")
        previous.reaction_ready.emit("happy")
        assert window.sprite.expression is None
        current.reaction_ready.emit("happy")
        assert window.sprite.expression == "happy"
        window.handle_interrupted()
        assert window.sprite.expression is None
        window.messages.append({"role": "user", "content": "Thanks"})
        window.handle_response("The model failed to load.")
        assert not window._celebrate_reply
    finally:
        window.chat_worker = None
        window.close()

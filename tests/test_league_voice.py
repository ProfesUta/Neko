import time
from threading import Event

import pytest

from core.context import Context
from core.league_metrics import LeagueMetrics
from core.league_voice import LeagueAnnouncement, LeagueVoicePolicy
from gui.workers import AdviceWorker, ChatWorker, LeagueWorker


def announcement(key="pick-locked", text="Ahri locked in.", priority=1, lifetime=6):
    return LeagueAnnouncement(text, key, time.monotonic(), lifetime, priority)


def test_voice_policy_priority_cooldown_bounded_queue_and_obsolete_drop(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.league_voice.time.monotonic", lambda: clock[0])
    policy = LeagueVoicePolicy(5)
    low = announcement("select", "Champion select.", 1)
    high = announcement("end", "Victory!", 3)
    assert policy.offer(low) and policy.offer(high)
    assert policy.pending == high and not policy.offer(low)
    assert policy.take(False) is None  # A user turn always takes priority.
    assert policy.take(True) == high and not policy.offer(high)
    assert policy.offer(low) and policy.take(True) is None
    clock[0] += 7
    assert policy.take(True) is None and policy.pending is None


def test_league_acknowledgments_share_tts_and_measure_actual_playback(monkeypatch):
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    context = Context()
    context.league.metrics = LeagueMetrics(True)
    item = announcement()
    object.__setattr__(item, "queued_ns", context.league.metrics.start())
    monkeypatch.setattr(context.league, "announcement_current", lambda advice: True)
    calls = []
    def speak(text, **kwargs):
        kwargs["before_playback"]()
        kwargs["on_playback_start"]()
        calls.append(text)
    monkeypatch.setattr("voice.tts.speak", speak)
    AdviceWorker([], context, item).run()
    assert calls == [item.text]
    assert context.league.metrics.report()["notification_to_audio"]["count"] == 1


def test_user_request_during_synthesis_cancels_event_before_audio(monkeypatch):
    from core.cancellation import OperationCancelled
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    context = Context()
    monkeypatch.setattr(context.league, "announcement_current", lambda advice: True)
    calls = []
    def speak(text, **kwargs):
        context.interaction.begin_user_turn()  # User speaks while WAV is generated.
        with pytest.raises(OperationCancelled):
            kwargs["before_playback"]()
        calls.append("cancelled")
    monkeypatch.setattr("voice.tts.speak", speak)
    AdviceWorker([], context, announcement()).run()
    assert calls == ["cancelled"]


def test_disabled_or_stale_game_comments_never_start_tts(monkeypatch):
    context = Context()
    monkeypatch.setattr(context.league, "announcement_current", lambda advice: True)
    monkeypatch.setattr("voice.tts.speak", lambda *args, **kwargs: pytest.fail("Unexpected audio"))
    AdviceWorker([], context, announcement()).run()
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    AdviceWorker([], context, announcement(lifetime=-1)).run()


@pytest.mark.parametrize("question,expected", [
    ("What champion am I hovering?", "hovering Ahri"),
    ("Who did I lock in?", "locked in Ahri"),
    ("What role did I get?", "support"),
    ("Which champions have been picked?", "Ahri"),
    ("What champions were banned?", "Yasuo"),
    ("What just happened in my game?", "Dragon"),
    ("Did we take Dragon?", "Your team took"),
    ("What was the last objective?", "Dragon"),
])
def test_deterministic_questions_skip_ollama_vision_and_spatial(question, expected, monkeypatch):
    from core.league_champ_select import ChampionSelectSnapshot
    from core.league_events import MatchEvent
    monkeypatch.setattr("config.LEAGUE_CHAMP_SELECT_ENABLED", True)
    monkeypatch.setattr("config.LEAGUE_EVENTS_ENABLED", True)
    context = Context()
    context.league.champ_select._snapshot = ChampionSelectSnapshot(
        "ChampSelect", "UTILITY", "Ahri", question == "Who did I lock in?", (), (), ("Ahri",), (), ("Yasuo",), time.monotonic(), True)
    context.league._events = (MatchEvent(1, "DragonKill", 500, time.monotonic(), "Yuumi", side="ally"),)
    context.league._events_at = time.monotonic()
    context.league._events_available = True
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Unexpected model"))
    monkeypatch.setattr(context, "refresh_screen", lambda *args, **kwargs: pytest.fail("Unexpected screen"))
    responses, errors = [], []
    worker = ChatWorker([], context, question)
    worker.response_ready.connect(responses.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and len(responses) == 1 and expected in responses[0]


def test_no_unsupported_result_or_team_claim(monkeypatch):
    from core.league_events import MatchEvent
    monkeypatch.setattr("config.LEAGUE_EVENTS_ENABLED", True)
    client = Context().league
    client._events = (MatchEvent(1, "DragonKill", 20, time.monotonic(), "Unknown"),)
    client._events_available, client._events_at = True, time.monotonic()
    assert "can't confirm which team" in client.answer("Did we take Dragon?")
    assert "recent match history" in client.answer("Did we take Baron?")
    assert client.answer("What build should I use?") is None
    assert client.context_for("Tell me about cats") is None


def test_league_worker_cancels_inflight_io_and_joins(qapp, wait_until):
    import asyncio
    context = Context()
    entered, cancelled = Event(), Event()
    async def run():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    context.league.run = run
    worker = LeagueWorker(context.league)
    worker.start()
    wait_until(entered.is_set)
    worker.stop()
    wait_until(lambda: not worker.isRunning())
    assert cancelled.is_set() and context.league.on_notification is None
    worker.deleteLater()


def test_window_owns_and_stops_league_worker_without_blocking(qapp, wait_until, monkeypatch):
    import asyncio
    from gui.window import CapriWindow
    monkeypatch.setattr("config.LEAGUE_EVENTS_ENABLED", True)
    context = Context()
    entered = Event()
    async def run():
        entered.set()
        await asyncio.Event().wait()
    context.league.run = run
    window = CapriWindow([], context)
    wait_until(entered.is_set)
    window.close()
    wait_until(lambda: window.league_worker is None)
    assert window._closing


def test_initial_locked_selection_and_reconnect_do_not_repeat_acknowledgment(monkeypatch):
    from core.league_champ_select import ChampionSelectSnapshot
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    league = Context().league
    notifications = []
    league.on_notification = notifications.append
    empty = ChampionSelectSnapshot(phase="ChampSelect", connected=True)
    hover = ChampionSelectSnapshot(phase="ChampSelect", my_champion="Ahri", connected=True)
    locked = ChampionSelectSnapshot(phase="ChampSelect", my_champion="Ahri", my_pick_locked=True, connected=True)
    league._champion_changed(empty, locked)  # Connecting halfway through select is silent.
    assert notifications == []
    league._champion_changed(hover, locked)
    assert len(notifications) == 1
    league._champion_changed(ChampionSelectSnapshot(), empty)
    league._champion_changed(hover, locked)  # Replayed transition after reconnect.
    assert len(notifications) == 1


def test_game_notice_speaks_with_microphone_and_vision_off(qapp, wait_until, monkeypatch):
    from tests.test_gui import make_window
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    spoken = []
    monkeypatch.setattr("voice.tts.speak", lambda text, **kwargs: spoken.append(text))
    window = make_window(qapp)
    monkeypatch.setattr(window.context.league, "announcement_current", lambda item: True)
    try:
        assert window.voice_worker is None and window.vision_worker is None
        assert window.advice_button.isChecked()
        item = announcement("objective:DragonKill:1", "Your team took Dragon.")
        window.handle_league_notification(item)
        wait_until(lambda: window.advice_worker is None)
        assert spoken == [item.text] and not window.chat_panel.isVisible()
    finally:
        window.close()


def test_game_notice_is_not_consumed_by_a_muted_listener(qapp, wait_until, monkeypatch):
    from tests.test_gui import make_window
    from gui.workers import VoiceWorker
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    spoken = []
    monkeypatch.setattr("voice.tts.speak", lambda text, **kwargs: spoken.append(text))
    window = make_window(qapp)
    listener = VoiceWorker(window.messages, window.context)
    listener.mute_microphone()
    window.voice_worker = listener
    monkeypatch.setattr(window.context.league, "announcement_current", lambda item: True)
    try:
        item = announcement()
        window.handle_league_notification(item)
        assert window._league_voice_policy.pending == item and not spoken
        window.voice_worker = None
        window._deliver_league_notification()
        wait_until(lambda: window.advice_worker is None)
        assert spoken == [item.text]
    finally:
        window.voice_worker = None
        window.close()


def test_advice_switch_silences_game_notices_even_with_vision_off(qapp, monkeypatch):
    from tests.test_gui import make_window
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    monkeypatch.setattr("voice.tts.speak", lambda *args, **kwargs: pytest.fail("Advice disabled"))
    window = make_window(qapp)
    try:
        assert window.advice_button.isChecked() and window.vision_worker is None
        window.toggle_advice()
        window.handle_league_notification(announcement())
        assert not window._advice_enabled and window.advice_worker is None
        assert window.advice_button.text() == "ADVICE OFF"
    finally:
        window.close()

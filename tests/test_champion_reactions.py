import time
from dataclasses import replace

import pytest

from core.league import LeagueClient
from core.league_champ_select import ChampionSelectSnapshot
from core.league_voice import HOVER_REACTIONS, LOCK_REACTIONS, LeagueVoicePolicy
from tests.test_gui import make_window


def selection(champion=None, locked=False, phase="ChampSelect"):
    return ChampionSelectSnapshot(phase=phase, my_champion=champion, my_pick_locked=locked,
                                  connected=True, updated_at=time.monotonic())


def client_with_notices(monkeypatch):
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    client = LeagueClient()
    notices = []
    client.on_notification = notices.append
    return client, notices


def transition(client, old, new):
    client.champ_select._snapshot = new
    client._champion_changed(old, new)


def test_hover_gets_a_named_playful_line_and_lock_gets_a_dry_ack(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    empty, hover, locked = selection(), selection("Ahri"), selection("Ahri", True)
    transition(client, empty, hover)
    transition(client, hover, locked)
    assert [item.event_key for item in notices] == ["pick-hover", "pick-locked"]
    assert notices[0].text == HOVER_REACTIONS[0].format(champion="Ahri")
    assert notices[1].text == LOCK_REACTIONS[0].format(champion="Ahri")
    assert notices[0].not_before == pytest.approx(notices[0].captured_at + .35)
    assert notices[1].champion == "Ahri" and client.announcement_current(notices[1])
    assert not client.announcement_current(notices[0])


def test_repeated_lcu_updates_and_rehover_do_not_spam(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    empty, ahri, jinx = selection(), selection("Ahri"), selection("Jinx")
    transition(client, empty, ahri)
    for _ in range(10):
        transition(client, ahri, replace(ahri, updated_at=time.monotonic()))
    transition(client, ahri, jinx)
    transition(client, jinx, ahri)
    assert len(notices) == 2 and notices[0].text != notices[1].text


def test_reaction_variants_continue_across_selects_and_state_resets(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    empty, hover, locked = selection(), selection("Ahri"), selection("Ahri", True)
    transition(client, empty, hover)
    transition(client, hover, locked)
    transition(client, locked, selection(phase="Lobby"))
    transition(client, empty, hover)
    transition(client, hover, locked)
    assert len(notices) == 4
    assert notices[0].text != notices[2].text and notices[1].text != notices[3].text


def test_hover_cancellation_unknown_champions_and_select_exit(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    transition(client, selection(), selection("Champion #103"))
    assert not notices
    hover = selection("Ahri")
    transition(client, selection(), hover)
    notice = notices[0]
    transition(client, hover, selection("Jinx"))
    assert not client.announcement_current(notice)
    transition(client, selection("Jinx"), selection(phase="Lobby"))
    assert not any(client.announcement_current(item) for item in notices)


def test_hover_debounce_coalesces_and_lock_bypasses_hover_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.league_voice.time.monotonic", lambda: clock[0])
    client, notices = client_with_notices(monkeypatch)
    transition(client, selection(), selection("Ahri"))
    transition(client, selection("Ahri"), selection("Jinx"))
    policy = LeagueVoicePolicy()
    assert policy.offer(notices[0]) and policy.offer(notices[1])
    assert policy.take(True) is None
    clock[0] += .36
    assert policy.take(True).champion == "Jinx"
    transition(client, selection("Jinx"), selection("Jinx", True))
    assert policy.offer(notices[-1])
    assert policy.take(True).event_key == "pick-locked"


def test_lock_replaces_hover_before_debounce_finishes(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    transition(client, selection(), selection("Ahri"))
    transition(client, selection("Ahri"), selection("Ahri", True))
    policy = LeagueVoicePolicy()
    policy.offer(notices[0])
    policy.offer(notices[1])
    assert policy.take(True) == notices[1] and policy.pending is None


def test_direct_lock_from_a_verified_empty_selection_still_reacts(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    empty = replace(selection(), selection_known=True)
    transition(client, empty, selection("Ahri", True))
    assert len(notices) == 1 and notices[0].event_key == "pick-locked"


def test_lock_before_queued_hover_speaks_only_the_lock_line_with_mic_off(qapp, wait_until, monkeypatch):
    window = make_window(qapp)
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    spoken = []
    monkeypatch.setattr("voice.tts.speak", lambda text, **kwargs: spoken.append(text))
    client = window.context.league
    client.on_notification = window.handle_league_notification
    try:
        transition(client, selection(), selection("Ahri"))
        assert not spoken and window._league_notice_timer.isActive()
        transition(client, selection("Ahri"), selection("Ahri", True))
        wait_until(lambda: window.advice_worker is None and bool(spoken))
        assert spoken == [LOCK_REACTIONS[0].format(champion="Ahri")]
        assert window.voice_worker is None and window.voice_button.text() == "MIC OFF"
        assert not window._league_notice_timer.isActive()
    finally:
        window.close()


def test_hover_timer_speaks_the_verified_pick_without_a_model(qapp, wait_until, monkeypatch):
    window = make_window(qapp)
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Reaction invoked the model"))
    spoken = []
    monkeypatch.setattr("voice.tts.speak", lambda text, **kwargs: spoken.append(text))
    client = window.context.league
    client.on_notification = window.handle_league_notification
    try:
        transition(client, selection(), selection("Ahri"))
        wait_until(lambda: window.advice_worker is None and bool(spoken))
        assert spoken == [HOVER_REACTIONS[0].format(champion="Ahri")]
        assert window.voice_worker is None
    finally:
        window.close()

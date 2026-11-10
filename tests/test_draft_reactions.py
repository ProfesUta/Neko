import copy
import time
import wave
from dataclasses import replace

import pytest

from core.league_champ_select import ChampionSelectSnapshot
from core.league_voice import LeagueVoicePolicy
from tests.test_champion_reactions import client_with_notices, transition
from tests.test_gui import make_window


def draft(allied=(), enemy=(), champion=None, locked=False, phase="ChampSelect", known=True):
    return ChampionSelectSnapshot(phase=phase, my_champion=champion, my_pick_locked=locked,
                                  allied_champions=allied, enemy_champions=enemy,
                                  connected=True, selection_known=known, updated_at=time.monotonic())


@pytest.mark.parametrize("side,name", [("allied", "Yuumi"), ("enemy", "Kai'Sa")])
def test_new_confirmed_picks_name_champion_and_side_once(monkeypatch, side, name):
    client, notices = client_with_notices(monkeypatch)
    before = draft()
    after = draft(**{side: (name,)})
    transition(client, before, after)
    for _ in range(10):
        transition(client, after, replace(after, updated_at=time.monotonic()))
    assert len(notices) == 1
    item = notices[0]
    assert item.event_key == "draft-picks" and name in item.text
    assert getattr(item, side + "_picks") == (name,)
    assert ("our" if side == "allied" else "they") in item.text.lower()
    assert client.announcement_current(item)


def test_lcu_hover_becomes_a_team_reaction_only_after_confirmed_lock(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    client.champ_select.names.names = {103: "Ahri", 222: "Jinx", 238: "Zed"}
    session = {
        "localPlayerCellId": 1,
        "myTeam": [{"cellId": 1}, {"cellId": 2, "championId": 222, "championPickIntent": 222}],
        "theirTeam": [{"cellId": 6, "championId": 238}],
        "actions": [[{"type": "pick", "actorCellId": 2, "championId": 222, "completed": False},
                     {"type": "pick", "actorCellId": 6, "championId": 238, "completed": False}]],
    }
    client.champ_select.update(phase="ChampSelect", session=copy.deepcopy(session))
    assert not notices
    session["actions"][0][0]["completed"] = True
    client.champ_select.update(session=copy.deepcopy(session))
    assert len(notices) == 1 and notices[0].allied_picks == ("Jinx",)
    assert not notices[0].enemy_picks and "Zed" not in notices[0].text
    session["actions"][0][1]["completed"] = True
    client.champ_select.update(session=copy.deepcopy(session))
    assert len(notices) == 2 and notices[1].enemy_picks == ("Zed",)


def test_local_player_is_not_announced_again_as_a_teammate(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    transition(client, draft(champion="Ahri"), draft(("Ahri",), champion="Ahri", locked=True))
    assert [notice.event_key for notice in notices] == ["pick-locked"]


def test_unknown_champions_and_unconfirmed_hovers_are_not_announced(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    before = draft()
    after = replace(draft(("Champion #99999",), ("Champion #88888",)),
                    allied_hovers=("Ahri",), enemy_hovers=("Zed",))
    transition(client, before, after)
    assert not notices


def test_multiple_picks_in_one_update_are_one_brief_reaction(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    transition(client, draft(), draft(("Jinx", "Lee Sin"), ("Zed", "Nautilus")))
    assert len(notices) == 1
    item = notices[0]
    assert item.allied_picks == ("Jinx", "Lee Sin") and item.enemy_picks == ("Zed", "Nautilus")
    assert "Jinx and Lee Sin" in item.text and "Zed and Nautilus" in item.text


def test_bootstrap_and_reconnect_baseline_existing_picks_silently(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    before, after = draft(known=False), draft(("Jinx",), ("Zed",))
    transition(client, before, after)
    transition(client, after, draft(phase="Unavailable", known=False))
    transition(client, draft(phase="Unavailable", known=False), draft(known=False))
    reconnected = draft(("Jinx", "Lee Sin"), ("Zed", "Nautilus"))
    transition(client, draft(known=False), reconnected)
    assert not notices
    transition(client, reconnected, draft(("Jinx", "Lee Sin"), ("Zed", "Nautilus", "Ahri")))
    assert len(notices) == 1 and notices[0].enemy_picks == ("Ahri",)


def test_reordered_or_temporarily_missing_picks_and_trades_do_not_replay(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    before = draft(("Ahri", "Jinx"), champion="Ahri", locked=True)
    transition(client, draft(known=False), before)
    traded = draft(("Jinx", "Ahri"), champion="Jinx", locked=True)
    transition(client, before, traded)
    transition(client, traded, draft(("Jinx",), champion="Jinx", locked=True))
    transition(client, draft(("Jinx",), champion="Jinx", locked=True), traded)
    assert not notices


def test_new_champion_select_can_react_to_same_champions_again(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    after = draft(("Jinx",), ("Zed",))
    transition(client, draft(), after)
    transition(client, after, draft(phase="Lobby", known=False))
    transition(client, draft(), after)
    assert len(notices) == 2 and notices[0].text != notices[1].text


def test_disabled_notifications_do_not_replay_picks_when_enabled_later(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", False)
    after = draft(("Jinx",), ("Zed",))
    transition(client, draft(), after)
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    transition(client, after, after)
    assert not notices


def test_fast_updates_coalesce_without_extending_old_deadlines(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.league_voice.time.monotonic", lambda: clock[0])
    client, notices = client_with_notices(monkeypatch)
    transition(client, draft(), draft(("Jinx",)))
    clock[0] += .05
    transition(client, draft(("Jinx",)), draft(("Jinx",), ("Zed",)))
    policy = LeagueVoicePolicy()
    assert policy.offer(notices[0]) and policy.offer(notices[1])
    assert policy.pending.allied_picks == ("Jinx",) and policy.pending.enemy_picks == ("Zed",)
    assert policy.pending.captured_at == 100.0 and policy.pending.lifetime == 10
    assert policy.take(True) is None
    clock[0] += .16
    assert policy.take(False) is None  # User conversation still owns speech.
    item = policy.take(True)
    assert "Jinx" in item.text and "Zed" in item.text and policy.pending is None


def test_confirmed_picks_follow_other_reactions_without_idle_cooldown_and_keep_own_lock_priority(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.league_voice.time.monotonic", lambda: clock[0])
    client, notices = client_with_notices(monkeypatch)
    transition(client, draft(champion="Ahri"), draft(("Ahri",), champion="Ahri", locked=True))
    transition(client, draft(("Ahri",), champion="Ahri", locked=True),
               draft(("Ahri", "Jinx"), champion="Ahri", locked=True))
    policy = LeagueVoicePolicy()
    assert policy.offer(notices[0]) and not policy.offer(notices[1])
    assert policy.take(True) == notices[0]
    assert policy.offer(notices[1])
    clock[0] += .16
    assert policy.take(True) == notices[1]


def test_hover_cannot_replace_a_pending_confirmed_team_pick(monkeypatch):
    client, notices = client_with_notices(monkeypatch)
    transition(client, draft(), draft(("Jinx",)))
    transition(client, draft(("Jinx",)), draft(("Jinx",), champion="Ahri"))
    policy = LeagueVoicePolicy()
    assert policy.offer(notices[0]) and not policy.offer(notices[1])
    assert policy.pending == notices[0]


@pytest.mark.parametrize("after", [draft(), draft(phase="Lobby"), draft(phase="Unavailable", known=False)])
def test_removed_picks_and_select_exit_invalidate_pending_speech(monkeypatch, after):
    client, notices = client_with_notices(monkeypatch)
    current = draft(("Jinx",), ("Zed",))
    transition(client, draft(), current)
    item = notices[0]
    assert client.announcement_current(item)
    transition(client, current, after)
    assert not client.announcement_current(item)


def test_teammate_and_enemy_names_reach_speech_with_mic_off_and_no_model(qapp, wait_until, monkeypatch):
    from voice import tts
    from voice.kokoro_session import session
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Draft reaction invoked the model"))
    monkeypatch.setattr(tts, "_require_kokoro", lambda: None)
    generated, played = [], []
    def generate(*args, **kwargs):
        generated.append(kwargs["text"])
        with wave.open(str(kwargs["output"]), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 240)
    def play(path, cancellation, on_playback_start=None):
        with wave.open(str(path), "rb") as audio:
            assert audio.getnframes() == 240
        if on_playback_start:
            on_playback_start()
        played.append(True)
    monkeypatch.setattr(session, "request", generate)
    monkeypatch.setattr(tts, "_play_wav", play)
    window = make_window(qapp)
    client = window.context.league
    client.on_notification = window.handle_league_notification
    try:
        first = draft(("Yuumi",))
        transition(client, draft(), first)
        transition(client, first, draft(("Yuumi",), ("K'Sante",)))
        wait_until(lambda: window.advice_worker is None and bool(played))
        assert len(generated) == len(played) == 1
        assert "Yuumi" in generated[0] and "KSante" in generated[0]
        transcript = window.chat_panel.transcript.toPlainText()
        assert "Yuumi" in transcript and "K'Sante" in transcript
        assert window.voice_worker is None and window.voice_button.text() == "MIC OFF"
        assert not window.chat_panel.isVisible()
    finally:
        window.close()

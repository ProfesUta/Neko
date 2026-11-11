import asyncio
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import ollama
import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QBitmap, QImage, QPainter, QRegion, QKeyEvent

import config
from core.cancellation import OperationCancelled, check_cancelled
from core.context import Context
from core.league import LeagueClient, LeagueSession
from core.spatial import ActionTicket
from core.spatial_geometry import Monitor, Rect
from core.window_grounding import VisibleWindow
from gui.sprites import CharacterSprite
from gui.workers import ChatWorker, LeagueWorker
from tests.test_gui import make_window


@pytest.mark.parametrize("phase", ["ChampSelect", "GameStart", "InProgress", "Reconnect"])
def test_active_gameflow_activates_without_voice_or_advice(phase):
    league = LeagueClient()
    changed = []
    league.on_session_active_changed = changed.append
    league.champ_select.update(phase=phase)
    league.champ_select.update(phase=phase)
    assert league.session_active and changed == [True]
    assert not config.LEAGUE_PROACTIVE_VOICE_ENABLED


def test_champ_select_delete_loading_transition_and_outage_have_grace():
    state = LeagueSession()
    assert state.observe(0, 1, phase="ChampSelect")
    assert not state.observe(1, 1, phase="None") and state.active
    assert not state.observe(2, 1, phase="GameStart") and state.active
    assert not state.observe(3, 1, phase="Unavailable") and state.active
    assert not state.observe(8, 1) and state.active
    assert state.observe(14, 1) and not state.active


def test_leaving_champ_select_for_lobby_is_inactive_even_with_client_connected():
    league = LeagueClient()
    changed = []
    league.on_session_active_changed = changed.append
    league.champ_select.update(phase="ChampSelect")
    league.champ_select.update(phase="Lobby")
    assert not league.session_active and league.champ_select.snapshot().connected
    assert changed == [True, False]


def test_startup_mid_match_baselines_events_silently_but_activates_companion():
    league = LeagueClient()
    changes, notices = [], []
    league.on_session_active_changed, league.on_notification = changes.append, notices.append
    data = {"Events": [{"EventID": 0, "EventName": "GameStart", "EventTime": 0},
                       {"EventID": 1, "EventName": "ChampionKill", "EventTime": 50}]}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as http:
            assert await league.poll_events(http)
            assert await league.poll_events(http)
    asyncio.run(run())
    assert league.session_active and changes == [True] and notices == []


def test_game_end_is_terminal_even_if_api_and_lcu_still_expose_old_game():
    league = LeagueClient()
    changes = []
    league.on_session_active_changed = changes.append
    league.champ_select.update(phase="InProgress")
    data = {"Events": [{"EventID": 0, "EventName": "GameStart", "EventTime": 0},
                       {"EventID": 1, "EventName": "GameEnd", "EventTime": 800}]}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as http:
            await league.poll_events(http)
            await league.poll_events(http)
    asyncio.run(run())
    league.champ_select.update(phase="InProgress")
    league.champ_select.update(phase="EndOfGame")
    league.champ_select.update(phase="Lobby")
    league._observe_session(live=True)
    assert not league.session_active and changes == [True, False]


def test_short_http_failure_keeps_mode_then_inactivity_expires(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("core.league.time.monotonic", lambda: clock[0])
    league = LeagueClient()
    changes = []
    league.on_session_active_changed = changes.append
    league._observe_session(live=True)
    async def fail(request):
        raise httpx.ConnectError("fixture")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
            clock[0] = 1
            assert not await league.poll_events(http)
            assert league.session_active
            clock[0] = 6
            assert not await league.poll_events(http)
            assert league.session_active
            clock[0] = 12
            assert not await league.poll_events(http)
    asyncio.run(run())
    assert not league.session_active and changes == [True, False]


def test_multiple_matches_and_old_ended_api_during_new_select():
    league = LeagueClient()
    changes = []
    league.on_session_active_changed = changes.append
    for _ in range(3):
        league.champ_select.update(phase="ChampSelect")
        league._observe_session(ended=True)  # Last game's endpoint can linger.
        assert league.session_active
        league.champ_select.update(phase="GameStart")
        league._observe_session(ended=True)
        assert not league.session_active
        league.champ_select.update(phase="Lobby")
    assert changes == [True, False] * 3


def test_worker_activity_signal_is_independent_and_window_scan_is_not_polled(qapp):
    league = LeagueClient()
    calls, active, displays = [], [], []
    window = VisibleWindow(1, 2, "League", "League of Legends.exe", Rect(-1920, 0, 1920, 1080))
    def windows(cancel_event):
        calls.append(True)
        return [window]
    worker = LeagueWorker(league, SimpleNamespace(windows=windows))
    worker.session_active_changed.connect(active.append)
    worker.session_display_changed.connect(displays.append)
    async def run():
        league.champ_select.update(phase="ChampSelect")
        for _ in range(20):
            league.champ_select.update(phase="ChampSelect")
        league.champ_select.update(phase="InProgress")
        league._observe_session(ended=True)
    league.run = run
    worker.run()
    assert active == [True, False] and displays == [window.bounds] and len(calls) == 1
    assert league.on_session_active_changed is None and league.on_gameflow_changed is None


def image_bounds(sprite):
    image = QImage(sprite.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    sprite.render(image)
    return QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()


def test_actual_svg_assets_share_feet_and_right_edge_without_breathing_or_reactions(qapp):
    sprite = CharacterSprite()
    sprite.resize(300, 300)
    try:
        identities = {name: id(renderer) for name, renderer in sprite._renderers.items()}
        sprite.set_league_mode(True)
        assert sprite._frames == ("gamelooking",)
        looking = image_bounds(sprite)
        for status in ("idle", "processing", "speaking", "listening", "happy"):
            sprite.set_activity(status)
            sprite._advance()
            assert sprite._frames == ("gamelooking",)
        assert not sprite.react("happy") and not sprite.flash_expression("playful")
        assert not sprite.start_pointing("left")
        sprite.set_league_speaking(True)
        talking = image_bounds(sprite)
        assert sprite._frames == ("gametalking",)
        assert abs(looking.bottom() - talking.bottom()) <= 1
        assert abs(looking.right() - talking.right()) <= 1
        assert abs(looking.height() - talking.height()) <= 3
        assert looking.width() < talking.width()
        assert {name: id(renderer) for name, renderer in sprite._renderers.items()} == identities
        assert not sprite._timer.isActive()
        sprite.set_league_speaking(False)
        sprite.set_activity("listening")
        sprite.set_league_mode(False)
        assert sprite._frames != ("gamelooking",) and sprite.state == "listening" and sprite._timer.isActive()
    finally:
        sprite.stop_animation()


def test_svg_layout_handles_different_viewboxes_and_origins_without_stretch(qapp, monkeypatch, tmp_path):
    assets = {
        "idle.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100"/></svg>',
        "GameLooking.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 400"><rect x="80" y="30" width="80" height="350" fill="red"/></svg>',
        "GameTalking.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="100 200 400 800"><rect x="220" y="260" width="200" height="700" fill="blue"/></svg>',
    }
    for name, text in assets.items():
        (tmp_path / name).write_text(text)
    monkeypatch.setattr("gui.sprites.SPRITE_DIRECTORY", tmp_path)
    sprite = CharacterSprite()
    sprite.resize(300, 300)
    try:
        sprite.set_league_mode(True)
        first = image_bounds(sprite)
        sprite.set_league_speaking(True)
        second = image_bounds(sprite)
        assert abs(first.height() - second.height()) <= 1
        assert abs(first.right() - second.right()) <= 1 and abs(first.bottom() - second.bottom()) <= 1
        assert first.width() / first.height() == pytest.approx(80 / 350, abs=.01)
        assert second.width() / second.height() == pytest.approx(200 / 700, abs=.01)
    finally:
        sprite.stop_animation()


def test_missing_league_assets_keep_normal_svg_rendering(qapp, monkeypatch, tmp_path, caplog):
    (tmp_path / "idle.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="40"/></svg>')
    monkeypatch.setattr("gui.sprites.SPRITE_DIRECTORY", tmp_path)
    sprite = CharacterSprite()
    try:
        sprite.set_league_mode(True)
        sprite.set_league_speaking(True)
        assert sprite._frames == ("idle",) and not image_bounds(sprite).isEmpty()
        assert "keeping Capri's normal sprites" in caplog.text
    finally:
        sprite.stop_animation()


def monitor(identity="main", x=0, y=0, width=1920, height=1040, scale=1, primary=True):
    logical = Rect(x, y, width, height)
    return Monitor(identity, 1 if primary else 2, identity, logical,
                   Rect(x, y, width * scale, height * scale), logical, scale, primary)


@pytest.fixture
def companion(qapp, monkeypatch):
    window = make_window(qapp)
    window.show()
    qapp.processEvents()
    window._movement_timer.stop()
    screens = [monitor()]
    monkeypatch.setattr(window.monitor_manager, "snapshot", lambda: tuple(screens))
    yield window, screens
    window.close()
    qapp.processEvents()


@pytest.mark.parametrize("screen", [monitor(), monitor(x=-1600, y=-300, width=1600, height=860),
                                    monitor(x=1920, y=100, width=1707, height=960, scale=1.5),
                                    monitor(width=320, height=200)])
def test_docking_centers_visible_art_and_keeps_right_margin_in_logical_coordinates(companion, screen):
    window, screens = companion
    screens[:] = [screen]
    previous = QPoint(window.pos())
    window.set_league_companion_mode(True)
    body = window._league_visible_in_window().translated(QPointF(window.pos()))
    assert body.right() == pytest.approx(screen.available.right - config.LEAGUE_DOCK_MARGIN_PX, abs=1)
    assert body.center().y() == pytest.approx(screen.available.center[1], abs=1)
    assert body.left() >= screen.available.x and body.top() >= screen.available.y
    assert body.bottom() <= screen.available.bottom
    assert window.sprite._frames == ("gamelooking",) and window.movement_locked
    if window.pos() != previous:
        assert len(window.teleport_effects.bursts) == 2
    at = QPoint(window.pos())
    window.set_league_companion_mode(True)
    assert window.pos() == at and len(window.teleport_effects.bursts) <= 2


def test_game_monitor_selection_topology_dpi_and_disconnect_reanchor_without_vfx(companion):
    window, screens = companion
    screens.append(monitor("left", -2560, -100, 1707, 960, 1.5, False))
    window._league_display_changed(Rect(-2400, 0, 1800, 1000))
    window.set_league_companion_mode(True)
    assert window._league_monitor_id == "left" and window.x() < 0
    window.teleport_effects.stop()
    screens[1] = replace(screens[1], available=Rect(-2560, -100, 1280, 680), logical=Rect(-2560, -100, 1280, 720), scale=2)
    window._on_monitors_changed()
    body = window._league_visible_in_window().translated(QPointF(window.pos()))
    assert body.right() == pytest.approx(-1280 - 8, abs=1) and not window.teleport_effects.bursts
    screens.pop()
    window._on_monitors_changed()
    assert window._league_monitor_id == "main" and not window.teleport_effects.bursts


def test_league_lock_blocks_every_existing_movement_path_and_positional_tools(companion, monkeypatch):
    window, screens = companion
    window.set_league_companion_mode(True)
    original = QPoint(window.pos())
    window.teleport_effects.stop()
    monkeypatch.setattr("gui.window.random.randint", lambda *args: pytest.fail("League mode chose a random destination"))
    window._choose_movement_target()
    window._set_movement_state("processing")
    window._next_wander_at = window._next_processing_teleport_at = 0
    window._move_pet()
    window.teleport_randomly()
    window.move(20, 20)
    assert not window.teleport_to(20, 20)
    ticket = ActionTicket("show", 1, threading.Event())
    window.movement_coordinator._execute(ticket)
    assert ticket.done.is_set() and ticket.result["status"] == "unavailable"
    assert "keeps me docked" in ticket.result["message"]
    assert window.pos() == original and not window.teleport_effects.bursts


def test_league_exit_unlocks_and_resets_schedules_without_forcing_movement(companion, monkeypatch):
    window, screens = companion
    for _ in range(3):
        window.set_league_companion_mode(True)
        original = QPoint(window.pos())
        window.set_status("Listening")
        window.set_league_companion_mode(False)
        assert not window.movement_locked and not window.sprite.league_mode
        assert window.sprite.state == "listening" and window.sprite._frames != ("gamelooking",)
        assert window.pos() == original and window._movement_target == original
        assert window._next_wander_at > time.monotonic() and window._glitch_steps == 0
    window.set_status("Standby")
    target = QPoint(40, 40)
    monkeypatch.setattr(window, "_choose_movement_target", lambda *args: setattr(window, "_movement_target", target))
    window._next_wander_at = 0
    window._move_pet()
    assert window.pos() == target


def test_speaking_status_cannot_select_talking_until_actual_tts_callback(companion, qapp):
    window, screens = companion
    window.set_league_companion_mode(True)
    worker = ChatWorker(window.messages, window.context, "Hello", True)
    window.chat_worker = worker
    window._connect_worker(worker)
    try:
        window.set_status("Speaking")  # Synthesis is about to start, not sound.
        assert window.sprite._frames == ("gamelooking",)
        worker._set_playback_active(True)
        qapp.processEvents()
        assert window.sprite._frames == ("gametalking",)
        window.set_status("Processing")
        assert window.sprite._frames == ("gametalking",)
        worker.stop()
        qapp.processEvents()
        assert window.sprite._frames == ("gamelooking",)
    finally:
        window.chat_worker = None
        worker.deleteLater()


@pytest.mark.parametrize("prefetch,native", [(False, False), (True, False), (True, True)])
def test_streamed_tts_is_one_talking_interval_including_automatic_replies(companion, qapp, wait_until, monkeypatch, prefetch, native):
    from core import conversation
    from voice import tts
    window, screens = companion
    window.set_league_companion_mode(True)
    monkeypatch.setattr(config, "CONVERSATION_STREAMING_ENABLED", True)
    monkeypatch.setattr(config, "TTS_PREFETCH_ENABLED", prefetch)
    monkeypatch.setattr(config, "TTS_NATIVE_STREAMING_ENABLED", native)
    monkeypatch.setattr(conversation, "conversation_model", lambda: "fixture")
    first, second, release = threading.Event(), threading.Event(), threading.Event()
    played, states = [], []
    def play(text, options):
        options["on_playback_start"]()
        played.append(text)
        if len(played) == 1:
            first.set()
        else:
            second.set()
            while not release.wait(.005):
                check_cancelled(options["cancel_event"])
    class Prepared:
        def __init__(self, text, options):
            self.text, self.options = text, options
        def play(self):
            play(self.text, self.options)
        def close(self):
            pass
    monkeypatch.setattr(tts, "prepare_speech", lambda text, **options: Prepared(text, options))
    monkeypatch.setattr(tts, "speak", lambda text, **options: play(text, options))
    def stream_speech(text, ready, **options):
        ready(Prepared(text, options))
        ready(Prepared(text, options))
    monkeypatch.setattr(tts, "stream_speech", stream_speech)
    def generate(**kwargs):
        kwargs["on_text"]("Gravity pulls objects toward each other. ")
        assert first.wait(2)
        kwargs["on_text"]("Mass and distance affect the strength.")
        return ollama.ChatResponse(message={"role": "assistant", "content": "Gravity pulls objects toward each other. Mass and distance affect the strength."})
    monkeypatch.setattr(conversation, "request_chat", generate)
    window.send_text("Explain gravity", True)
    worker = window.chat_worker
    worker.playback_active_changed.connect(states.append)
    try:
        wait_until(lambda: second.is_set() and window.sprite._frames == ("gametalking",))
        assert states == [True]
        release.set()
        wait_until(lambda: window.chat_worker is None)
        assert states == [True, False] and len(played) == (4 if native else 2)
        assert window.sprite._frames == ("gamelooking",)
    finally:
        release.set()
        if window.chat_worker is not None:
            window.chat_worker.stop()
            wait_until(lambda: window.chat_worker is None)


def test_shutting_down_docked_window_never_reanchors_or_restarts_animation(companion, qapp):
    window, screens = companion
    window.set_league_companion_mode(True)
    at = QPoint(window.pos())
    window.close()
    window.set_league_companion_mode(False)
    window._on_monitors_changed()
    qapp.processEvents()
    assert window.pos() == at and not window.sprite._timer.isActive()
    assert not window.teleport_effects.bursts


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
def test_automatic_league_tts_uses_actual_speaking_pose_with_mic_off(companion, wait_until, monkeypatch, outcome):
    from core.league_voice import LeagueAnnouncement
    from voice import tts
    window, screens = companion
    window.set_league_companion_mode(True)
    monkeypatch.setattr(config, "LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    monkeypatch.setattr(window.context.league, "announcement_current", lambda item: True)
    started, release = threading.Event(), threading.Event()
    def speak(text, **options):
        options["on_playback_start"]()
        started.set()
        while not release.wait(.005):
            check_cancelled(options["cancel_event"])
        if outcome == "failed":
            raise RuntimeError("Fixture speaker error")
    monkeypatch.setattr(tts, "speak", speak)
    window.handle_league_notification(LeagueAnnouncement("Your team locked Yuumi.", "fixture", time.monotonic()))
    try:
        wait_until(lambda: started.is_set() and window.sprite._frames == ("gametalking",))
        assert window.voice_worker is None and window.voice_button.text() == "MIC OFF"
        if outcome == "cancelled":
            window.stop_current_turn()
        else:
            release.set()
        wait_until(lambda: window.advice_worker is None)
        assert window.sprite._frames == ("gamelooking",) and window.movement_locked
        assert not window.chat_panel.isVisible()
    finally:
        release.set()
        if window.advice_worker is not None:
            window.advice_worker.stop()
            wait_until(lambda: window.advice_worker is None)


def test_qt_session_updates_are_queued_and_native_window_lookup_stays_on_league_worker(companion, wait_until):
    window, screens = companion
    screens.append(monitor("left", -1920, 0, 1920, 1040, 1, False))
    window._advice_enabled = False
    calls, produced, finish = [], threading.Event(), threading.Event()
    def windows(cancellation):
        calls.append(threading.get_ident())
        return [VisibleWindow(1, 2, "Game", "League of Legends.exe", Rect(-1920, 0, 1920, 1080))]
    async def run():
        window.context.league.champ_select.update(phase="Reconnect")
        produced.set()
        while not finish.is_set():
            await asyncio.sleep(.005)
    window.context.league.run = run
    worker = LeagueWorker(window.context.league, SimpleNamespace(windows=windows))
    window.league_worker = worker
    worker.session_active_changed.connect(window.set_league_companion_mode, Qt.ConnectionType.QueuedConnection)
    worker.session_display_changed.connect(window._league_display_changed, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(window.league_worker_finished)
    worker.start()
    try:
        assert produced.wait(2)
        assert not window.league_mode_active  # No Qt events have been processed yet.
        wait_until(lambda: window.league_mode_active)
        assert window._league_monitor_id == "left"
        assert calls and all(thread != threading.get_ident() for thread in calls)
        finish.set()
        wait_until(lambda: window.league_worker is None)
        assert not window.league_mode_active and not window.movement_locked
    finally:
        finish.set()
        if window.league_worker is not None:
            worker.stop()
            wait_until(lambda: window.league_worker is None)


def test_game_window_appearing_after_select_reanchors_without_second_lightning(companion, qapp):
    window, screens = companion
    screens.append(monitor("left", -1920, 0, 1920, 1040, 1, False))
    windows = []
    worker = LeagueWorker(window.context.league, SimpleNamespace(windows=lambda cancellation: windows))
    worker.session_active_changed.connect(window.set_league_companion_mode, Qt.ConnectionType.QueuedConnection)
    worker.session_display_changed.connect(window._league_display_changed, Qt.ConnectionType.QueuedConnection)
    worker._session_changed(True)
    qapp.processEvents()
    assert window._league_monitor_id == "main"
    window.teleport_effects.stop()
    windows.append(VisibleWindow(1, 2, "Game", "League of Legends.exe", Rect(-1920, 0, 1920, 1080)))
    worker._gameflow_changed("GameStart")
    qapp.processEvents()
    assert window._league_monitor_id == "left" and window.x() < 0
    assert not window.teleport_effects.bursts


def test_live_api_readiness_resolves_a_late_game_window_once_without_rescanning_on_polls(qapp):
    league = LeagueClient()
    windows, scans, displays = [], [], []
    def scan(cancellation):
        scans.append(True)
        return windows
    worker = LeagueWorker(league, SimpleNamespace(windows=scan))
    league.on_session_active_changed = worker._session_changed
    league.on_gameflow_changed = worker._gameflow_changed
    league.on_live_game_ready = worker._live_game_ready
    worker.session_display_changed.connect(displays.append)
    league.champ_select.update(phase="InProgress")
    assert len(scans) == 1  # No immediate duplicate from the phase callback.
    game = VisibleWindow(1, 2, "Game", "League of Legends.exe", Rect(-1920, 0, 1920, 1080))
    windows.append(game)
    for _ in range(20):
        league._observe_session(live=True)
    assert len(scans) == 2 and displays == [game.bounds]


def test_ctrl_t_is_suppressed_only_during_league_and_global_teleport_config_is_unchanged(companion, monkeypatch):
    window, screens = companion
    enabled = config.TELEPORT_ENABLED
    shortcut = next(shortcut for shortcut in window._shortcuts if shortcut.key().toString() == "Ctrl+T")
    window.set_league_companion_mode(True)
    at = QPoint(window.pos())
    shortcut.activated.emit()
    assert window.pos() == at and config.TELEPORT_ENABLED == enabled
    window.set_league_companion_mode(False)
    target = QPoint(40, 40)
    monkeypatch.setattr(window, "_choose_movement_target", lambda *args: setattr(window, "_movement_target", target))
    shortcut.activated.emit()
    assert window.pos() == target and config.TELEPORT_ENABLED == enabled


def test_league_paint_and_speech_switching_never_reparse_svg_or_rescan_alpha(qapp, monkeypatch):
    sprite = CharacterSprite()
    try:
        sprite.set_league_mode(True)
        monkeypatch.setattr("gui.sprites.QSvgRenderer", lambda *args: pytest.fail("Reparsed SVG during speech"))
        monkeypatch.setattr("gui.sprites.silhouette_from_renderer", lambda *args: pytest.fail("Rescanned alpha during speech"))
        for _ in range(6):
            sprite.set_league_speaking(True)
            image_bounds(sprite)
            sprite.set_league_speaking(False)
            image_bounds(sprite)
    finally:
        sprite.stop_animation()

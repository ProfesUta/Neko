import httpx
import pytest

from core.league import LeagueClient, direct_league_answer, own_player_snapshot


def payloads():
    return {
        "activeplayer": {"riotId": "Player#123", "currentGold": 1350, "level": 8,
                         "championStats": {"currentHealth": 450, "maxHealth": 1000}},
        "playerlist": [
            {"riotId": "Opponent#456", "championName": "Ahri", "scores": {"kills": 99}},
            {"riotId": "Player#123", "championName": "Jinx", "isDead": False,
             "scores": {"kills": 3, "deaths": 1, "assists": 4, "creepScore": 80},
             "items": [{"displayName": "Boots"}]},
        ], "gamestats": {"gameTime": 600},
    }


def test_snapshot_only_uses_current_players_own_data():
    data = payloads()
    game = own_player_snapshot(data["activeplayer"], data["playerlist"], data["gamestats"])
    assert game["champion"] == "Jinx"
    assert game["scores"]["kills"] == 3
    assert "Ahri" not in str(game) and "Player#123" not in str(game)
    assert direct_league_answer("What's my health?", game) == "You have 450 of 1000 health, chum."
    assert direct_league_answer("How much gold do I have?", game) == "You've got 1350 gold."
    assert direct_league_answer("What's my KDA?", game) == "3 kills, 1 death, 4 assists."
    assert direct_league_answer("Should I fight Ahri now?", game) is None
    assert direct_league_answer("Where is the enemy jungler?", game) is None


def test_live_data_uses_fixed_local_gets_caches_briefly_and_clears_after_match(monkeypatch):
    real = httpx.Client
    clock = [100.0]
    requests = []
    playing = [True]
    data = payloads()
    def handle(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 2999
        assert request.method == "GET"
        if not playing[0]:
            return httpx.Response(404)
        return httpx.Response(200, json=data[request.url.path.rsplit("/", 1)[-1]])
    monkeypatch.setattr("core.league.httpx.Client", lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr("core.league.time.monotonic", lambda: clock[0])
    client = LeagueClient()
    assert client.read()["level"] == 8
    assert len(requests) == 3
    clock[0] += 0.5
    assert client.read()["age_seconds"] == 0.5 and len(requests) == 3
    clock[0] += 1.5
    playing[0] = False
    assert client.read() is None
    assert client.read() is None and len(requests) == 4


@pytest.mark.parametrize("question,answer", [
    ("How much gold do I have?", "You've got 1350 gold."),
    ("What level am I?", "You're level 8."),
    ("Who am I playing?", "You're playing Jinx, chum."),
    ("What's my score?", "3 kills, 1 death, 4 assists."),
])
def test_league_stats_bypass_both_models_and_screenshot(question, answer, monkeypatch):
    from core.context import Context
    from gui.workers import ChatWorker
    import config
    monkeypatch.setattr(config, "LEAGUE_LIVE_DATA_ENABLED", True)
    monkeypatch.setattr(config, "CONVERSATION_STREAMING_ENABLED", True)
    context = Context()
    context.set_screen_reader(object())
    data = payloads()
    game = own_player_snapshot(data["activeplayer"], data["playerlist"], data["gamestats"])
    monkeypatch.setattr(context.league, "read", lambda *args: game)
    monkeypatch.setattr(context, "refresh_screen", lambda *args, **kwargs: pytest.fail("Unexpected screenshot"))
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: pytest.fail("Unexpected model"))
    worker = ChatWorker([], context, question)
    responses, errors = [], []
    worker.response_ready.connect(responses.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and responses == [answer]


@pytest.mark.parametrize("health", [450, 80])
def test_background_league_awareness_keeps_visual_analysis_with_game_data(monkeypatch, health):
    import time
    import mss
    from PIL import Image
    from types import SimpleNamespace
    from gui.vision import CapturedScreen, VisionWorker
    monkeypatch.setattr("config.LEAGUE_LIVE_DATA_ENABLED", True)
    data = payloads()
    game = own_player_snapshot(data["activeplayer"], data["playerlist"], data["gamestats"])
    game["stats"]["currentHealth"] = health
    worker = VisionWorker(5, "test", (100, 100), 2)
    worker.set_advice_enabled(True)
    tips = []
    worker.advice_ready.connect(tips.append)
    worker.set_league_client(SimpleNamespace(read=lambda cancellation: game))
    class Capture:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(mss, "mss", Capture)
    monkeypatch.setattr(worker, "_take_frame", lambda *args: CapturedScreen(
        b"pixels", Image.new("L", (20, 20)), "League of Legends (TM) Client", "game", time.monotonic()))
    frames = []
    def analyze(frame, question, cancellation, allow_advice):
        frames.append(frame)
        assert frame.game["champion"] == "Jinx"
        assert bool(tips) == (health < 200)  # Own-health warnings precede model inference.
        assert allow_advice == (health >= 200)
        return "Visible League lane and minimap", None, None
    monkeypatch.setattr(worker, "_analyze", analyze)
    observations = []
    def observe(text, captured):
        observations.append(text)
        worker.stop()
    worker.observation_ready.connect(observe)
    worker.run()
    assert observations == ["Visible League lane and minimap"]
    assert len(frames) == 1 and frames[0].jpeg == b"pixels"


def test_background_vision_resumes_promptly_after_conversation(monkeypatch):
    from gui.vision import VisionWorker
    monkeypatch.setattr("gui.vision.time.monotonic", lambda: 100)
    worker = VisionWorker(5, "test", (100, 100), 2)
    assert worker._resume_after == 0
    worker.set_paused(True)
    worker.set_paused(False)
    assert worker._resume_after == 101
    worker.set_paused(False)
    assert worker._resume_after == 101


def test_vision_off_prevents_game_data_reads(monkeypatch):
    import ollama
    from core.context import Context
    from gui.workers import ChatWorker
    monkeypatch.setattr("config.LEAGUE_LIVE_DATA_ENABLED", True)
    monkeypatch.setattr("config.CONVERSATION_STREAMING_ENABLED", True)
    context = Context()
    monkeypatch.setattr(context.league, "read", lambda *args: pytest.fail("VISION is OFF"))
    monkeypatch.setattr("core.conversation.conversation_model", lambda: "test-chat")
    monkeypatch.setattr("core.conversation.request_chat", lambda **kwargs: ollama.ChatResponse(
        message={"role": "assistant", "content": "Turn VISION on so I can read your health."}))
    errors = []
    worker = ChatWorker([], context, "What's my health?")
    worker.error.connect(errors.append)
    worker.run()
    assert not errors

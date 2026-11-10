import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from core.league import LeagueClient
from core.league_events import EventTracker
from core.league_metrics import LeagueMetrics


def event(event_id, name="ChampionKill", when=10, **fields):
    return {"EventID": event_id, "EventName": name, "EventTime": when, **fields}


def test_initial_history_is_silent_new_batch_is_chronological_and_deduplicated():
    tracker = EventTracker(3)
    start = event(0, "GameStart", 0)
    assert tracker.ingest({"Events": [start]}, 1) == ()
    batch = [start, event(3, "BaronKill", 33), event(1, when=11), event(2, "DragonKill", 22)]
    new = tracker.ingest({"Events": batch}, 2)
    assert [item.event_id for item in new] == [1, 2, 3]
    assert [item.game_time for item in new] == [11, 22, 33]
    assert all(item.received_at == 2 for item in new)
    assert len(tracker.history) == 3 and tracker.ingest({"Events": batch}, 3) == ()
    # IDs evicted from the bounded history must still never replay.
    assert tracker.ingest({"Events": batch + [event(4, "TurretKilled", 44)]}, 4)[0].event_id == 4
    assert tracker.ingest({"Events": batch}, 5) == ()  # Rollback becomes a silent new baseline.


def test_idless_events_use_stable_fingerprint_and_survive_actor_enrichment():
    tracker = EventTracker(1)
    initial = {"EventName": "GameStart", "EventTime": 0}
    dragon = {"EventName": "DragonKill", "EventTime": 60, "KillerName": "User#1"}
    tracker.ingest({"Events": [initial]}, 1)
    assert len(tracker.ingest({"Events": [initial, dragon]}, 2)) == 1
    tracker.update_actors({"User#1": ("Yuumi", "ally")})
    assert tracker.history[0].side == "ally"
    assert tracker.ingest({"Events": [initial, dragon]}, 3) == ()
    turret1 = {"EventName": "TurretKilled", "EventTime": 80, "KillerName": "User#1", "TurretKilled": "A"}
    turret2 = {**turret1, "TurretKilled": "B"}
    assert len(tracker.ingest({"Events": [initial, dragon, turret2, turret1]}, 4)) == 2
    assert tracker.ingest({"Events": [initial, dragon, turret2, turret1]}, 5) == ()


def test_new_matches_and_game_time_resets_are_silently_baselined():
    tracker = EventTracker()
    tracker.ingest({"Events": [event(0, "GameStart", 0), event(8, when=800)]}, 1)
    tracker.set_game_time(900)
    before = tracker.epoch
    tracker.set_game_time(5)
    assert tracker.epoch == before + 1 and not tracker.history
    assert tracker.ingest({"Events": [event(0, "GameStart", .1)]}, 2) == ()
    assert tracker.ingest({"Events": [event(0, "GameStart", .1), event(1, "DragonKill", 300)]}, 3)
    # Game restart without a sampled game-time reset: full-history ID rollback.
    assert tracker.ingest({"Events": [event(0, "GameStart", .2)]}, 4) == ()
    assert len(tracker.history) == 1


@pytest.mark.parametrize("payload", [None, [], {}, {"Events": None}, {"Events": [{}]},
    {"Events": [event(True)]}, {"Events": [event(1, when=float("nan"))]},
    {"Events": [event(1, when=-1)]}, {"Events": [event(1, name="bad event")] }])
def test_invalid_event_responses_rejected(payload):
    if payload == {"Events": [event(True)]}:
        # A malformed ID has a valid timestamp/name and uses the safe fallback.
        tracker = EventTracker()
        tracker.ingest(payload, 0)
        assert tracker.history[0].event_id is None
    else:
        with pytest.raises(ValueError):
            EventTracker().ingest(payload, 0)


def test_end_result_requires_actual_api_evidence_and_clears_player_stats(monkeypatch):
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    client = LeagueClient(LeagueMetrics(True))
    notifications = []
    client.on_notification = notifications.append
    data = {"Events": [event(0, "GameStart", 0)]}
    def handle(request):
        return httpx.Response(200, json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            assert await client.poll_events(http)
            client._snapshot = {"level": 10}
            data["Events"].append(event(1, "GameEnd", 1800, Result="Win"))
            assert await client.poll_events(http)
            assert client._snapshot is None
            assert await client.poll_events(http)
    asyncio.run(run())
    assert len(notifications) == 1 and "victory" in notifications[0].text
    assert client.tracker.history[-1].describe() == "The match ended in a victory!"
    unknown = EventTracker()
    unknown.ingest({"Events": [event(1, "GameEnd", 1800, Result="made up")]}, 1)
    assert "hasn't supplied" in unknown.history[-1].describe()


def test_failure_clears_availability_recovery_does_not_replay_and_getters_are_copy_safe():
    client = LeagueClient()
    data = {"Events": [event(0, "GameStart", 0), event(1, "DragonKill", 100)]}
    fail = [False]
    def handle(request):
        assert request.url.host == "127.0.0.1" and request.url.port == 2999 and request.method == "GET"
        return httpx.Response(503) if fail[0] else httpx.Response(200, json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            await client.poll_events(http)
            assert len(client.events()) == 2
            fail[0] = True
            assert not await client.poll_events(http) and client.events() == ()
            fail[0] = False
            await client.poll_events(http)
            assert len(client.events()) == 2
            assert client.tracker.ingest(data, time.monotonic()) == ()
    asyncio.run(run())
    client._snapshot = {"stats": {"currentHealth": 10}}
    client._captured_at = time.monotonic()
    first = client.read()
    first["stats"]["currentHealth"] = 0
    assert client.read()["stats"]["currentHealth"] == 10
    client._captured_at -= 10
    assert client.read() is None


def test_stats_requests_coalesce_and_do_not_hold_publication_lock():
    client = LeagueClient()
    calls = []
    async def handle(request):
        calls.append(request.url.path)
        # Readers remain immediate while a request is suspended.
        with client._lock:
            assert client._snapshot is None
        await asyncio.sleep(.01)
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"gamestats": {"gameTime": 50},
            "activeplayer": {"riotId": "me", "championStats": {"currentHealth": 10}},
            "playerlist": [{"riotId": "me", "championName": "Yuumi", "team": "ORDER"}]}[name])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            await asyncio.gather(*(client.refresh_stats(http) for _ in range(8)))
    asyncio.run(run())
    assert len(calls) == 3 and client.read()["champion"] == "Yuumi"


def test_concurrent_readers_see_immutable_snapshots_during_publication():
    client = LeagueClient()
    def reader(_):
        for _ in range(100):
            value = client.champ_select.snapshot()
            assert isinstance(value.allied_champions, tuple)
            assert isinstance(client.events(), tuple)
            client.read()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(reader, i) for i in range(4)]
        for _ in range(100):
            client.champ_select.update(phase="ChampSelect", session={})
        for job in jobs:
            job.result()


def test_metrics_disabled_has_no_clock_overhead_and_enabled_is_bounded(monkeypatch):
    monkeypatch.setattr("core.league_metrics.perf_counter_ns", lambda: pytest.fail("Clock read when disabled"))
    disabled = LeagueMetrics(False)
    assert disabled.start() == 0
    disabled.finish("x", 0)
    assert disabled.report() == {}
    enabled = LeagueMetrics(True)
    for value in range(3000):
        enabled.record("parse", value * 1000)
    report = enabled.report()["parse"]
    assert report["count"] == 3000 and report["retained_samples"] == 2048
    assert report["p50_ms"] <= report["p95_ms"] <= report["p99_ms"] <= report["max_ms"]


def test_stats_resume_after_an_ended_match_time_resets():
    client = LeagueClient()
    client.tracker.set_game_time(1800)
    client.tracker.ingest({"Events": [event(0, "GameStart", 0), event(1, "GameEnd", 1800)]}, time.monotonic())
    before = client.tracker.epoch
    def handle(request):
        return httpx.Response(200, json={"gamestats": {"gameTime": 5},
            "activeplayer": {"riotId": "me", "championStats": {"currentHealth": 10}},
            "playerlist": [{"riotId": "me", "championName": "Yuumi", "team": "ORDER"}]}[request.url.path.rsplit("/", 1)[-1]])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            await client.refresh_stats(http)
    asyncio.run(run())
    assert client.tracker.epoch == before + 1 and client.read()["champion"] == "Yuumi"
    assert not client.events()


def test_failed_concurrent_stats_refresh_is_also_coalesced():
    client = LeagueClient()
    requests = []
    async def handle(request):
        requests.append(request)
        await asyncio.sleep(.01)
        return httpx.Response(503)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            await asyncio.gather(*(client.refresh_stats(http) for _ in range(8)))
    asyncio.run(run())
    assert len(requests) == 1 and client.read() is None


def test_new_objective_notifies_once_but_existing_history_stays_silent(monkeypatch):
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    client = LeagueClient()
    notifications = []
    client.on_notification = notifications.append
    data = {"Events": [event(0, "GameStart", 0), event(1, "DragonKill", 300)]}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as http:
            await client.poll_events(http)
            assert notifications == []
            data["Events"].append(event(2, "BaronKill", 1400))
            await client.poll_events(http)
            await client.poll_events(http)
    asyncio.run(run())
    assert len(notifications) == 1 and "Baron" in notifications[0].text


def test_only_verified_own_kills_and_deaths_generate_personal_reactions(monkeypatch):
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED", True)
    client = LeagueClient()
    notifications = []
    client.on_notification = notifications.append
    data = {"gamestats": {"gameTime": 50},
            "activeplayer": {"riotId": "me#1", "championStats": {"currentHealth": 100}},
            "playerlist": [{"riotId": "me#1", "championName": "Yuumi", "team": "ORDER"},
                           {"riotId": "other#2", "championName": "Ahri", "team": "CHAOS"}],
            "eventdata": {"Events": [event(0, "GameStart", 0)]}}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json=data[request.url.path.rsplit("/", 1)[-1]]))) as http:
            await client.refresh_stats(http)
            await client.poll_events(http)
            data["eventdata"]["Events"].append(event(1, when=5, KillerName="me#1", VictimName="other#2"))
            await client.poll_events(http)
            data["eventdata"]["Events"].append(event(2, when=6, KillerName="other#2", VictimName="third#3"))
            await client.poll_events(http)
            data["eventdata"]["Events"].append(event(3, when=7, KillerName="other#2", VictimName="me#1"))
            await client.poll_events(http)
            await client.poll_events(http)
    asyncio.run(run())
    assert len(notifications) == 2
    assert notifications[0].event_key == "own-kill:1" and "kill" in notifications[0].text
    assert notifications[1].event_key == "own-death:3" and "went down" in notifications[1].text
    assert all("#" not in notification.text for notification in notifications)


def test_ambiguous_short_names_do_not_identify_the_local_player():
    client = LeagueClient()
    data = {"gamestats": {"gameTime": 50},
            "activeplayer": {"riotId": "Shared#1", "riotIdGameName": "Shared", "championStats": {}},
            "playerlist": [{"riotId": "Shared#1", "riotIdGameName": "Shared", "championName": "Yuumi"},
                           {"riotId": "Shared#2", "riotIdGameName": "Shared", "championName": "Ahri"}]}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json=data[request.url.path.rsplit("/", 1)[-1]]))) as http:
            await client.refresh_stats(http)
    asyncio.run(run())
    assert "Shared#1" in client._own_aliases and "Shared" not in client._own_aliases

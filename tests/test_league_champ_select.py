import asyncio
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import httpx
import pytest

from core.league_champ_select import (ChampionNames, ChampionSelectClient, Credentials,
                                      LockfileDiscovery, PHASE_PATH, SELECT_PATH,
                                      champion_snapshot, parse_lockfile)
from core.league_metrics import LeagueMetrics


def names(tmp_path):
    result = ChampionNames(tmp_path / "names.json")
    result.names = {103: "Ahri", 64: "Lee Sin", 222: "Jinx", 238: "Zed", 157: "Yasuo"}
    return result


def session(locked=False, champion=103):
    return {"localPlayerCellId": 1,
            "myTeam": [{"cellId": 1, "assignedPosition": "middle", "championPickIntent": champion,
                        "championId": champion if locked else 0, "summonerId": "NEVER_EXPOSE_THIS"},
                       {"cellId": 2, "championId": 64}],
            "theirTeam": [{"cellId": 6, "championId": 238, "puuid": "HIDDEN_ID"}],
            "actions": [[{"type": "pick", "actorCellId": 1, "championId": champion, "completed": locked},
                         {"type": "pick", "actorCellId": 2, "championId": 64, "completed": True},
                         {"type": "pick", "actorCellId": 6, "championId": 238, "completed": True}]],
            "bans": {"myTeamBans": [157, -1], "theirTeamBans": []}}


def packet(path, data, event_type="Update"):
    return json.dumps([8, "OnJsonApiEvent", {"uri": path, "data": data, "eventType": event_type}])


def test_parse_lockfile_restricts_protocol_port_and_hides_secrets():
    credentials = parse_lockfile("LeagueClient:1234:54321:secret-token:https\n")
    assert credentials == Credentials(1234, 54321, "secret-token")
    assert credentials.authorization == "Basic cmlvdDpzZWNyZXQtdG9rZW4="
    assert "secret-token" not in repr(credentials)


@pytest.mark.parametrize("text", ["", "riot:12:44:secret:https", "LeagueClient:1:0:secret:https",
    "LeagueClient:1:65536:secret:https", "LeagueClient:1:44:secret:http",
    "LeagueClient:0:44:secret:https", "LeagueClient:1:44::https",
    "LeagueClient:1:44:abc\nsecret:https", "LeagueClient:1:44:with:colon:https", "x" * 4097])
def test_malformed_lockfiles_fail_without_echoing_input(text):
    with pytest.raises(ValueError, match="^Invalid League lockfile$"):
        parse_lockfile(text)


def test_discovery_fallback_and_client_restart_changes_port(tmp_path, monkeypatch):
    lock = tmp_path / "lockfile"
    monkeypatch.setattr("core.league_champ_select.psutil.process_iter", lambda *args: [])
    lock.write_text("LeagueClient:1:12345:first:https")
    discovery = LockfileDiscovery([str(tmp_path)])
    assert discovery.discover().port == 12345
    lock.write_text("LeagueClient:2:23456:second:https")
    assert discovery.discover().port == 23456
    lock.unlink()
    assert discovery.discover() is None


def test_process_executable_discovery_does_not_read_command_lines(tmp_path, monkeypatch):
    (tmp_path / "lockfile").write_text("LeagueClient:1:12345:secret:https")
    monkeypatch.setattr("core.league_champ_select.psutil.process_iter", lambda attributes: [
        SimpleNamespace(info={"name": "LeagueClientUx.exe", "exe": str(tmp_path / "LeagueClientUx.exe")})])
    assert LockfileDiscovery().discover().port == 12345


def test_hover_lock_team_bans_exit_and_copy_safety(tmp_path):
    metrics = LeagueMetrics(True)
    client = ChampionSelectClient(metrics, names=names(tmp_path))
    client.handle_message(packet(PHASE_PATH, "ChampSelect"))
    source = session()
    client.handle_message(packet(SELECT_PATH, source))
    first = client.snapshot()
    assert first.role == "MIDDLE" and first.my_champion == "Ahri" and not first.my_pick_locked
    assert first.selection_known
    assert first.allied_champions == ("Lee Sin",) and first.enemy_champions == ("Zed",)
    assert first.allied_hovers == ("Ahri",) and first.bans == ("Yasuo",)
    assert "NEVER_EXPOSE_THIS" not in str(first.summary()) and "HIDDEN_ID" not in str(first)
    with pytest.raises(FrozenInstanceError):
        first.role = "TOP"
    source["myTeam"].clear()
    assert client.snapshot() == first
    client.handle_message(packet(SELECT_PATH, session(champion=222)))
    assert client.snapshot().my_champion == "Jinx"
    client.handle_message(packet(SELECT_PATH, session(locked=True)))
    assert client.snapshot().my_pick_locked
    client.handle_message(packet(PHASE_PATH, "InProgress"))
    assert client.snapshot().my_champion is None and client.snapshot().bans == ()
    assert metrics.report()["lcu_message_to_snapshot"]["count"] == 5


@pytest.mark.parametrize("data", [None, {}, {"myTeam": None, "actions": "bad", "bans": []},
    {"localPlayerCellId": "1", "myTeam": [None, {"championId": -1}], "theirTeam": 12,
     "actions": [None, [None, {}]], "bans": {"myTeamBans": "bad"}}])
def test_missing_select_fields_are_safe(tmp_path, data):
    snapshot = champion_snapshot("ChampSelect", data, names(tmp_path), 10)
    assert snapshot.my_champion is None and not snapshot.my_pick_locked


@pytest.mark.parametrize("raw", ["bad json", "{}", "[]", '[8,"Other",{}]',
    '[8,"OnJsonApiEvent",null]', '[8,"OnJsonApiEvent",{"uri":"other","data":{}}]'])
def test_malformed_or_unrelated_ws_messages_do_not_change_state(tmp_path, raw):
    client = ChampionSelectClient(LeagueMetrics(), names=names(tmp_path))
    before = client.snapshot()
    client.handle_message(raw)
    assert client.snapshot() == before


def test_delete_select_clears_old_hover(tmp_path):
    client = ChampionSelectClient(LeagueMetrics(), names=names(tmp_path))
    client.handle_message(packet(SELECT_PATH, session()))
    client.handle_message(packet(SELECT_PATH, None, "Delete"))
    assert client.snapshot().phase == "None" and client.snapshot().my_champion is None


def test_rest_bootstrap_auth_is_loopback_only_and_coalesced(tmp_path):
    client = ChampionSelectClient(LeagueMetrics(), names=names(tmp_path))
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "127.0.0.1"
        assert request.headers["Authorization"] == Credentials(1, 12345, "secret").authorization
        return httpx.Response(200, json="ChampSelect" if request.url.path == PHASE_PATH else session())
    async def run():
        async with httpx.AsyncClient(base_url="https://127.0.0.1:12345", transport=httpx.MockTransport(handle),
                                     headers={"Authorization": Credentials(1, 12345, "secret").authorization}) as http:
            await asyncio.gather(client.initial_state(http), client.initial_state(http))
    asyncio.run(run())
    assert len(requests) == 2 and client.snapshot().my_champion == "Ahri"


def test_ws_subscription_auth_push_and_reconnect(tmp_path, monkeypatch):
    client = ChampionSelectClient(LeagueMetrics(True), names=names(tmp_path))
    attempts, sent = [], []
    class Socket:
        async def send(self, text):
            sent.append(text)
        def __aiter__(self):
            return self.messages()
        async def messages(self):
            yield packet(SELECT_PATH, session(locked=True))
            raise OSError("Never log this secret")
    class Connection:
        async def __aenter__(self):
            return Socket()
        async def __aexit__(self, *args):
            pass
    def connect(uri, **options):
        attempts.append((uri, options))
        return Connection()
    async def bootstrap(http, **kwargs):
        client.update(phase="ChampSelect", session=session())
    async def sleep(delay):
        assert .4 <= delay <= .6
        if len(attempts) >= 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    monkeypatch.setattr(client, "initial_state", bootstrap)
    monkeypatch.setattr("core.league_champ_select.asyncio.sleep", sleep)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await client._websocket(Credentials(1, 12345, "secret"), object(), object())
    asyncio.run(run())
    assert len(attempts) == 2 and sent == ['[5,"OnJsonApiEvent"]'] * 2
    assert attempts[0][0] == "wss://127.0.0.1:12345/"
    assert attempts[0][1]["proxy"] is None
    assert attempts[0][1]["additional_headers"] == {"Authorization": Credentials(1, 12345, "secret").authorization}
    assert client._ws_connected is False


def test_static_names_are_cached_and_refreshed_only_once(tmp_path):
    mapping = names(tmp_path)
    mapping.loaded_at = 0
    requests = []
    def handle(request):
        requests.append(str(request.url))
        assert request.url.host == "ddragon.leagueoflegends.com"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=["16.1.1"] if request.url.path.endswith("versions.json")
                              else {"data": {"Ahri": {"key": "103", "name": "Ahri"}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            await mapping.refresh(http)
            await mapping.refresh(http)
    asyncio.run(run())
    assert len(requests) == 2 and mapping.name(103) == "Ahri"
    assert ChampionNames(mapping.path).name(103) == "Ahri"
    assert mapping.name(9999) == "Champion #9999"


def test_fallback_rest_clears_state_when_client_unavailable(tmp_path, monkeypatch):
    client = ChampionSelectClient(LeagueMetrics(), names=names(tmp_path))
    client.update(phase="ChampSelect", session=session())
    async def fail(http):
        raise httpx.ConnectError("secret")
    async def stop(delay):
        raise asyncio.CancelledError()
    monkeypatch.setattr(client, "initial_state", fail)
    monkeypatch.setattr("core.league_champ_select.asyncio.sleep", stop)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await client._fallback(object())
    asyncio.run(run())
    assert not client.snapshot().connected and client.snapshot().my_champion is None


def test_running_lcu_service_replaces_transport_after_port_change(tmp_path, monkeypatch):
    first, second = Credentials(1, 12345, "first-secret"), Credentials(2, 23456, "second-secret")
    discovery = SimpleNamespace(current=first)
    discovery.discover = lambda: discovery.current
    client = ChampionSelectClient(LeagueMetrics(), names=names(tmp_path), discovery=discovery)
    real_http, real_sleep = httpx.AsyncClient, asyncio.sleep
    ports, closed_ports = [], []
    requests = []
    def handle(request):
        requests.append(request)
        expected = first if request.url.port == first.port else second
        assert request.headers["Authorization"] == expected.authorization
        return httpx.Response(200, json="ChampSelect" if request.url.path == PHASE_PATH else session())
    monkeypatch.setattr("core.league_champ_select.httpx.AsyncClient", lambda **kwargs: real_http(
        **kwargs, transport=httpx.MockTransport(handle)))
    async def refresh(http):
        pass
    async def websocket(credentials, http, tls):
        ports.append(credentials.port)
        if credentials == first:
            discovery.current = second
        try:
            await asyncio.Event().wait()
        finally:
            closed_ports.append(credentials.port)
    async def fallback(http):
        await asyncio.Event().wait()
    async def sleep(seconds):
        await real_sleep(0)
        if len(ports) == 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(client.names, "refresh", refresh)
    monkeypatch.setattr(client, "_websocket", websocket)
    monkeypatch.setattr(client, "_fallback", fallback)
    monkeypatch.setattr("core.league_champ_select.asyncio.sleep", sleep)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await client.run()
    asyncio.run(run())
    assert ports == [12345, 23456] and closed_ports == ports
    assert {request.url.port for request in requests} == {12345, 23456}
    assert not client.snapshot().connected


def test_subscribe_bootstrap_forces_resync_even_after_a_recent_rest_read(tmp_path):
    client = ChampionSelectClient(LeagueMetrics(), names=names(tmp_path))
    requests = []
    data = session()
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json="ChampSelect" if request.url.path == PHASE_PATH else data)
    async def run():
        async with httpx.AsyncClient(base_url="https://127.0.0.1:12345", transport=httpx.MockTransport(handle)) as http:
            await client.initial_state(http)
            data["myTeam"][0]["championPickIntent"] = 222
            await client.initial_state(http, force=True)
    asyncio.run(run())
    assert len(requests) == 4 and client.snapshot().my_champion == "Jinx"

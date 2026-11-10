"""Read-only LCU champion select: lockfile, REST bootstrap, WAMP push, fallback.

Only fixed loopback URLs receive lockfile credentials. Never log exceptions or
objects from this module: third-party HTTP/WS debug logging may contain headers.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import ssl
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from threading import Lock

import httpx
import psutil

import config
from core.league_events import safe_text

PHASE_PATH = "/lol-gameflow/v1/gameflow-phase"
SELECT_PATH = "/lol-champ-select/v1/session"
QUIET_LOG = logging.Logger("capri.lcu.transport", level=logging.CRITICAL + 1)
QUIET_LOG.addHandler(logging.NullHandler())


def local_tls():
    if config.LEAGUE_ROOT_CERT:
        return ssl.create_default_context(cafile=config.LEAGUE_ROOT_CERT)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


@dataclass(frozen=True)
class Credentials:
    process_id: int
    port: int
    password: str = field(repr=False)

    @property
    def authorization(self):
        return "Basic " + base64.b64encode(("riot:" + self.password).encode()).decode("ascii")


def parse_lockfile(text):
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError("Invalid League lockfile")
    parts = text.strip().split(":")
    if len(parts) != 5:
        raise ValueError("Invalid League lockfile")
    name, pid, port, password, protocol = parts
    if (name not in ("LeagueClient", "LeagueClientUx") or not pid.isascii() or not pid.isdigit()
            or not port.isascii() or not port.isdigit() or int(pid) <= 0
            or not 1 <= int(port) <= 65535 or protocol != "https"
            or not password or len(password) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in password)):
        raise ValueError("Invalid League lockfile")
    return Credentials(int(pid), int(port), password)


class LockfileDiscovery:
    def __init__(self, paths=None):
        self._configured = tuple(config.LEAGUE_LOCKFILE_PATHS if paths is None else paths)
        self._auto_discover = paths is None
        self._found = None
        self._scan_at = 0.0
        self._candidates = []

    def discover(self):
        now = time.monotonic()
        if now >= self._scan_at:
            candidates = [Path(path).expanduser() for path in self._configured]
            if self._auto_discover:
                candidates = self._system_paths() + candidates
            self._candidates = list(dict.fromkeys(candidates))
            self._scan_at = now + 30
        for path in dict.fromkeys(([self._found] if self._found else []) + self._candidates):
            try:
                if path.is_dir():
                    path = path / "lockfile"
                if path.stat().st_size > 4096:
                    continue
                credentials = parse_lockfile(path.read_text(encoding="utf-8"))
                self._found = path
                return credentials
            except (OSError, ValueError, UnicodeError):
                continue
        return None

    @staticmethod
    def _system_paths():
        candidates = [Path(drive + "/Riot Games/League of Legends/lockfile") for drive in ("C:", "D:", "E:", "F:")]
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.getenv(variable):
                candidates.append(Path(os.environ[variable]) / "Riot Games/League of Legends/lockfile")
        try:
            for process in psutil.process_iter(["name", "exe"]):
                if (process.info.get("name") or "").lower() in ("leagueclient.exe", "leagueclientux.exe"):
                    if process.info.get("exe"):
                        candidates.insert(0, Path(process.info["exe"]).parent / "lockfile")
        except (psutil.Error, OSError):
            pass
        try:
            installs = Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "Riot Games/RiotClientInstalls.json"
            if installs.stat().st_size < 128_000:
                data = json.loads(installs.read_text(encoding="utf-8"))
                for executable in (data.get("associated_client") or {}):
                    if Path(executable).name.lower() == "leagueclient.exe":
                        candidates.append(Path(executable).parent / "lockfile")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return candidates


class ChampionNames:
    """Data Dragon ID/name map: load once, refresh at most daily, cache only changes."""
    def __init__(self, cache_path=None):
        self.path = cache_path or config.DATA_DIR / "league-champions.json"
        self.names = {}
        self.loaded_at = 0.0
        try:
            if self.path.stat().st_size < 200_000:
                cached = json.loads(self.path.read_text(encoding="utf-8"))
                self._load(cached.get("names", {}))
                self.loaded_at = self.path.stat().st_mtime
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _load(self, names):
        if isinstance(names, dict):
            self.names = {int(key): safe_text(value) for key, value in names.items()
                          if str(key).isdigit() and isinstance(value, str) and value.strip()}

    def name(self, champion_id):
        return self.names.get(champion_id, f"Champion #{champion_id}") if champion_id else None

    async def refresh(self, client):
        if self.names and time.time() - self.loaded_at < 86400:
            return
        try:
            response = await client.get("https://ddragon.leagueoflegends.com/api/versions.json")
            response.raise_for_status()
            if len(response.content) > 128_000:
                return
            versions = response.json()
            version = versions[0] if isinstance(versions, list) and versions else ""
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                return
            response = await client.get(f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json")
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                return
            data = response.json().get("data", {})
            if not isinstance(data, dict):
                return
            self._load({item.get("key"): item.get("name") for item in data.values() if isinstance(item, dict)})
            if not self.names:
                return
            self.loaded_at = time.time()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": version, "names": self.names}), encoding="utf-8")
            temporary.replace(self.path)
        except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError):
            pass  # ID labels are honest; retry next startup, never per hover.


@dataclass(frozen=True)
class ChampionSelectSnapshot:
    phase: str = "Unavailable"
    role: str = ""
    my_champion: str | None = None
    my_pick_locked: bool = False
    allied_champions: tuple = ()
    enemy_champions: tuple = ()
    allied_hovers: tuple = ()
    enemy_hovers: tuple = ()
    bans: tuple = ()
    updated_at: float = 0.0
    connected: bool = False
    selection_known: bool = False  # Distinguish a real empty selection from REST bootstrap.

    def summary(self):
        return asdict(self)


def champion_snapshot(phase, session, names, now):
    if phase != "ChampSelect" or not isinstance(session, dict):
        return ChampionSelectSnapshot(phase=phase, updated_at=now, connected=True)
    teams = [session.get("myTeam"), session.get("theirTeam")]
    teams = [team if isinstance(team, list) else [] for team in teams]
    local_cell = session.get("localPlayerCellId")
    local = next((player for player in teams[0] if isinstance(player, dict)
                  and type(local_cell) is int and player.get("cellId") == local_cell), {})
    actions = session.get("actions")
    actions = [action for group in actions if isinstance(group, list)
               for action in group if isinstance(action, dict)] if isinstance(actions, list) else []
    def positive(value):
        return value if type(value) is int and value > 0 else 0
    def pick(player):
        cell = player.get("cellId")
        action = next((action for action in reversed(actions)
                       if action.get("actorCellId") == cell and action.get("type") == "pick"), {})
        locked = action.get("completed") is True
        # championId can be a hover in some queues. Only a completed pick is locked.
        champion = (positive(player.get("championId")) or positive(action.get("championId"))) if locked else (
            positive(player.get("championPickIntent")) or positive(action.get("championId")) or positive(player.get("championId")))
        return names.name(champion), locked
    own_name, own_locked = pick(local)
    picked, hovers = [[], []], [[], []]
    for index, team in enumerate(teams):
        for player in team[:20]:
            if not isinstance(player, dict):
                continue
            name, locked = pick(player)
            if name:
                (picked if locked else hovers)[index].append(name)
    bans = session.get("bans") or {}
    banned_ids = []
    if isinstance(bans, dict):
        for key in ("myTeamBans", "theirTeamBans"):
            if isinstance(bans.get(key), list):
                banned_ids.extend(positive(value) for value in bans[key][:20])
    banned_ids.extend(positive(action.get("championId")) for action in actions
                      if action.get("type") == "ban" and action.get("completed") is True)
    return ChampionSelectSnapshot(phase, safe_text(local.get("assignedPosition"), 20).upper(),
                                  own_name, own_locked, tuple(picked[0]), tuple(picked[1]),
                                  tuple(hovers[0]), tuple(hovers[1]),
                                  tuple(names.name(value) for value in dict.fromkeys(banned_ids) if value), now, True, bool(local))


class ChampionSelectClient:
    def __init__(self, metrics, on_change=None, discovery=None, names=None):
        self.metrics, self.on_change = metrics, on_change
        self.discovery = discovery or LockfileDiscovery()
        self.names = names or ChampionNames()
        self._lock = Lock()
        self._snapshot = ChampionSelectSnapshot()
        self._phase, self._session = "Unavailable", None
        self._ws_connected = False
        self._initial_lock = asyncio.Lock()
        self._last_rest = float("-inf")
        self._rest_version = 0

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def update(self, phase=None, session=None, clear=False):
        old = self.snapshot()
        if phase is not None:
            self._phase = safe_text(phase, 40) or "Unavailable"
            if self._phase != "ChampSelect":
                self._session = None
        if clear:
            self._session = None
        elif isinstance(session, dict):
            self._session = session
        new = champion_snapshot(self._phase, self._session, self.names, time.monotonic())
        if self._phase == "Unavailable":
            new = ChampionSelectSnapshot(updated_at=new.updated_at)
        with self._lock:
            self._snapshot = new
        if self.on_change:
            self.on_change(old, new)

    def handle_message(self, raw):
        started = self.metrics.start()
        try:
            if len(raw) > 2_000_000:
                return
            packet = json.loads(raw)
            if not isinstance(packet, list) or len(packet) != 3 or packet[:2] != [8, "OnJsonApiEvent"]:
                return
            event = packet[2]
            if not isinstance(event, dict):
                return
            uri = event.get("uri")
            if uri == PHASE_PATH and isinstance(event.get("data"), str):
                self.update(phase=event["data"])
            elif uri == SELECT_PATH:
                if event.get("eventType") == "Delete":
                    self.update(phase="None", clear=True)
                elif isinstance(event.get("data"), dict):
                    self.update(phase="ChampSelect", session=event["data"])
            else:
                return
            self.metrics.finish("lcu_message_to_snapshot", started)
        except (ValueError, TypeError, KeyError):
            return

    async def initial_state(self, client, force=False):
        version = self._rest_version
        async with self._initial_lock:
            # Bootstrap, reconnect and fallback can coincide; share one read.
            if self._rest_version != version or (not force and time.monotonic() - self._last_rest < .25):
                return
            try:
                await self._fetch_state(client)
                self._last_rest = time.monotonic()
            finally:
                self._rest_version += 1

    async def _fetch_state(self, client):
        response = await client.get(PHASE_PATH)
        response.raise_for_status()
        phase = response.json()
        if not isinstance(phase, str):
            raise ValueError("Malformed gameflow phase")
        self.update(phase=phase)
        if phase == "ChampSelect":
            response = await client.get(SELECT_PATH)
            if response.status_code == 404:
                self.update(clear=True)
            else:
                response.raise_for_status()
                if len(response.content) > 2_000_000 or not isinstance(data := response.json(), dict):
                    raise ValueError("Malformed champion select")
                self.update(session=data)

    async def _websocket(self, credentials, client, tls):
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            return  # Core-only installations can still use the REST fallback.
        failures = 0
        while True:
            try:
                async with connect(f"wss://127.0.0.1:{credentials.port}/", ssl=tls,
                                   additional_headers={"Authorization": credentials.authorization},
                                   proxy=None, open_timeout=1, close_timeout=.25,
                                   ping_interval=15, ping_timeout=5, compression=None,
                                   max_size=2_000_000, max_queue=16, logger=QUIET_LOG) as websocket:
                    await websocket.send('[5,"OnJsonApiEvent"]')
                    await self.initial_state(client, force=True)  # Resync the subscribe/bootstrap gap.
                    self._ws_connected = True
                    failures = 0
                    async for message in websocket:
                        self.handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Never include transport exceptions/credentials in diagnostics.
            finally:
                self._ws_connected = False
                # A failed WebSocket does not invalidate a successful REST read.
                # Fallback REST and lockfile changes own availability/expiry.
            failures = min(failures + 1, 6)
            await asyncio.sleep(min(15, .5 * 2**(failures-1)) * random.uniform(.8, 1.2))

    async def _fallback(self, client):
        while True:
            if not self._ws_connected:
                try:
                    await self.initial_state(client)
                except (httpx.HTTPError, ValueError, TypeError):
                    self.update(phase="Unavailable", clear=True)
            await asyncio.sleep(max(1, config.LEAGUE_CHAMP_SELECT_FALLBACK_INTERVAL))

    async def run(self):
        # This coroutine shares the League worker's loop with live match polling.
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as static:
            names_task = asyncio.create_task(self.names.refresh(static))
            names_applied = False
            try:
                while True:
                    credentials = self.discovery.discover()
                    if credentials is None:
                        self.update(phase="Unavailable", clear=True)
                        await asyncio.sleep(2)
                        continue
                    tls = local_tls()
                    self._last_rest = float("-inf")
                    async with httpx.AsyncClient(base_url=f"https://127.0.0.1:{credentials.port}",
                                                headers={"Authorization": credentials.authorization},
                                                verify=tls, trust_env=False, follow_redirects=False,
                                                timeout=config.LEAGUE_REQUEST_TIMEOUT_SECONDS,
                                                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2)) as client:
                        try:
                            await self.initial_state(client)
                        except (httpx.HTTPError, ValueError, TypeError):
                            self.update(phase="Unavailable", clear=True)
                        tasks = [asyncio.create_task(self._websocket(credentials, client, tls)),
                                 asyncio.create_task(self._fallback(client))]
                        try:
                            while self.discovery.discover() == credentials:
                                if not names_applied and names_task.done() and self._session:
                                    self.update()  # Refresh ID labels after first static download.
                                    names_applied = True
                                await asyncio.sleep(2)
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            self.update(phase="Unavailable", clear=True)
            finally:
                names_task.cancel()
                await asyncio.gather(names_task, return_exceptions=True)

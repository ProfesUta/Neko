"""Read-only own-player context from Riot's local Live Client Data API."""

import math
import asyncio
import copy
import re
import time
from threading import Lock

import httpx

import config
from core.cancellation import check_cancelled
from core.league_events import EventTracker
from core.league_metrics import LeagueMetrics
from core.league_champ_select import ChampionSelectClient, local_tls

BASE_URL = "https://127.0.0.1:2999/liveclientdata/"
LEAGUE_LIVE_PHASES = frozenset(("GameStart", "InProgress", "Reconnect"))


class LeagueSession:
    """Worker-owned activity state; short transport gaps never end a session."""
    def __init__(self):
        self.active = False
        self.phase = "Unavailable"
        self.stage = "inactive"
        self.blocked_epoch = None
        self.uncertain_since = None

    def observe(self, now, epoch, *, phase=None, live=False, ended=False, shutdown=False):
        previous = self.active
        old_phase = self.phase
        if phase is not None:
            self.phase = phase
            if phase == "ChampSelect" and old_phase != phase:
                self.blocked_epoch = None
                self.stage = "select"
            elif phase in LEAGUE_LIVE_PHASES:
                if phase == "GameStart" and old_phase not in LEAGUE_LIVE_PHASES:
                    self.blocked_epoch = None
                self.stage = "live"
        terminal_phase = phase in ("WaitingForStats", "PreEndOfGame", "EndOfGame", "TerminatedInError")
        inactive_phase = phase in ("Lobby", "Matchmaking", "ReadyCheck")
        if shutdown:
            self.active, self.stage = False, "inactive"
        elif terminal_phase or inactive_phase:
            if terminal_phase or self.stage == "live":
                self.blocked_epoch = epoch  # Ignore the old game's still-readable HTTP data.
            self.active, self.stage = False, "inactive"
            self.uncertain_since = None
        elif ended and self.stage != "select":
            self.blocked_epoch = epoch
            self.active, self.stage = False, "inactive"
            self.uncertain_since = None
        elif live and not ended and epoch != self.blocked_epoch:
            self.active = True
            self.stage = "live"
            self.uncertain_since = None
        elif self.phase in LEAGUE_LIVE_PHASES or self.phase == "ChampSelect":
            self.active = epoch != self.blocked_epoch
            self.uncertain_since = None
        elif self.active:
            if self.uncertain_since is None:
                self.uncertain_since = now
            if now - self.uncertain_since >= max(1, config.LEAGUE_SESSION_GRACE_SECONDS):
                self.active, self.stage = False, "inactive"
        return previous != self.active


def league_question(text, recent=(), champion_names=()):
    if re.search(r"\b(league(?: of legends)?|lol|champion|summoner|jungl\w*|gank|baron|"
                 r"kda|creep score|laning|lock(?:ed)? in|my game|my match|who am i playing)\b", text, re.I):
        return True
    if re.search(r"\b(?:website|python|programming|javascript|health insurance|healthcare|recipe|spreadsheet|salary)\b", text, re.I):
        return False
    if re.search(r"\b(?:play|lane|build|items?|skill|ability|beat|vs|against|matchup|counter|q|w|e|r)\b", text, re.I) and any(
           re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)
           for name in champion_names if isinstance(name, str) and len(name) >= 3):
        return True
    if re.search(r"\b(my (?:health|hp|mana|gold|kda|score|cs|level|role|position)|"
                 r"what level am i|how much (?:health|hp|gold) do i have|what role did i get|"
                 r"what lane did i get|champions? (?:have been |were |are )?(?:picked|banned)|"
                 r"champion.*hover|last objective|(?:we|my team|our team).{0,18}(?:dragon|baron|herald))\b", text, re.I):
        return True
    # Generic words like 'build', 'level', 'health' and 'items' must not turn
    # unrelated topics into League conversations. Only genuine user turns count.
    return bool(re.search(r"\b(what should i do|fight|retreat|recall|play|buy|push|wave|matchup|build|items|dragon)\b", text, re.I)
                and any(re.search(r"\b(league|champion|lol|lane)\b", item.get("content", ""), re.I)
                        for item in recent[-6:] if item.get("role") == "user"))


def _numbers(data, names):
    if not isinstance(data, dict):
        return {}
    return {key: value for key in names if type(value := data.get(key)) in (int, float)
            and math.isfinite(value) and value >= 0}


def own_player_snapshot(active, players, game):
    if not isinstance(active, dict) or not isinstance(active.get("championStats"), dict):
        return None
    identity = active.get("riotId") or active.get("summonerName")
    own = next((player for player in (players or []) if isinstance(player, dict) and identity
                and identity in (player.get("riotId"), player.get("summonerName"))), {})
    result = {"source": "League Live Client Data API; own player only",
              **_numbers(active, ("currentGold", "level")),
              "stats": _numbers(active["championStats"], ("currentHealth", "maxHealth", "resourceValue",
                                "resourceMax", "attackDamage", "abilityPower", "armor", "magicResist")),
              "game": _numbers(game, ("gameTime", "mapNumber")),
              "scores": _numbers(own.get("scores", {}), ("kills", "deaths", "assists", "creepScore", "wardScore"))}
    if isinstance(own.get("championName"), str):
        result["champion"] = own["championName"][:80]
    result["items"] = [item["displayName"][:100] for item in (own.get("items") or [])
                       if isinstance(item, dict) and isinstance(item.get("displayName"), str)][:8]
    result["item_ids"] = [item["itemID"] for item in (own.get("items") or [])
                          if isinstance(item,dict) and type(item.get("itemID")) is int and 0 < item["itemID"] < 100000][:8]
    if type(own.get("isDead")) is bool:
        result["isDead"] = own["isDead"]
    return result


class LeagueClient:
    """One background producer; cached copy-safe reads never perform network I/O."""
    def __init__(self, metrics=None):
        self._lock = Lock()
        self._snapshot = None
        self._captured_at = 0.0
        self.metrics = metrics or LeagueMetrics()
        self.tracker = EventTracker(config.LEAGUE_EVENT_HISTORY_LIMIT)
        self._events = ()
        self._events_at = 0.0
        self._events_available = False
        self._actors = {}
        self._own_aliases = frozenset()
        self.on_notification = None
        self.on_session_active_changed = None
        self.on_gameflow_changed = None
        self.on_live_game_ready = None
        self._session_live_epoch = None
        self._session = LeagueSession()
        self._acknowledged_picks = set()
        self._reacted_hovers = set()
        self._observed_allied_picks = set()
        self._observed_enemy_picks = set()
        self._hover_reaction_index = 0
        self._lock_reaction_index = 0
        self._draft_reaction_index = 0
        self.champ_select = ChampionSelectClient(self.metrics, self._champion_changed)
        self._stats_lock = None
        self._stats_completed_at = float("-inf")
        from core.league_knowledge import LeagueKnowledge
        from core.league_journal import MatchJournal
        self.knowledge = LeagueKnowledge(self.champ_select.names)
        self.journal = MatchJournal()
        self.journal.reset(self.tracker.epoch)
        self._roster = ()
        from core.league_lane import LaneTracker
        self.on_visual_state_changed = None
        self.visual_announcement_current = None
        self.lane = LaneTracker(config.LEAGUE_VISION_FALLBACK_ROLE,self._visual_changed)

    def _visual_changed(self, state):
        if self.on_visual_state_changed:
            self.on_visual_state_changed(state)

    def visual_context(self):
        """No I/O; capture requires a fresh match, public roster and known mode."""
        state = self.lane.snapshot()
        with self._lock:
            fresh=(self._snapshot is not None and self._session.active and state.epoch==self.tracker.epoch
                   and time.monotonic()-self._captured_at<=max(2,config.LEAGUE_CACHE_SECONDS*3))
        return state if fresh else None

    @property
    def champ_select_enabled(self):
        return config.LEAGUE_CHAMP_SELECT_ENABLED

    @property
    def events_enabled(self):
        return config.LEAGUE_EVENTS_ENABLED

    @property
    def stats_enabled(self):
        return config.LEAGUE_LIVE_DATA_ENABLED

    def _champion_changed(self, old, new):
        self.lane.phase(new.phase,new.role,new.selection_known)
        if new.phase not in ("ChampSelect", "Unavailable"):
            self._acknowledged_picks.clear()
            self._reacted_hovers.clear()
            self._observed_allied_picks.clear()
            self._observed_enemy_picks.clear()
        if new.phase in ("GameStart", "InProgress") and old.phase not in ("GameStart", "InProgress", "Reconnect", "Unavailable"):
            with self._lock:
                self.tracker.reset()
                self.journal.reset(self.tracker.epoch)
                self._roster = ()
                self._events = ()
                self._events_available = False
                self._snapshot = None
                self._actors = {}
                self._own_aliases = frozenset()
        if new.phase not in ("GameStart", "InProgress", "Reconnect", "Unavailable"):
            with self._lock:
                self._snapshot = None
        self._observe_session(phase=new.phase)
        if new.phase != old.phase and self.on_gameflow_changed:
            self.on_gameflow_changed(new.phase)
        if self.on_notification:
            from core.league_voice import HOVER_REACTIONS, LOCK_REACTIONS
            if new.phase == "ChampSelect" and old.phase not in ("ChampSelect", "Unavailable"):
                self._notify("champ-select", "Champion select's up. Let's see your pick.", 1, 5)
            if (new.phase == "ChampSelect" and not new.my_pick_locked and new.my_champion
                    and not new.my_champion.startswith("Champion #")
                    and (new.my_champion != old.my_champion or old.phase != "ChampSelect")
                    and new.my_champion not in self._reacted_hovers):
                self._reacted_hovers.add(new.my_champion)
                text = HOVER_REACTIONS[self._hover_reaction_index % len(HOVER_REACTIONS)].format(champion=new.my_champion)
                self._hover_reaction_index += 1
                self._notify("pick-hover", text, 1, 8, champion=new.my_champion, delay_seconds=.35)
            if (new.phase == "ChampSelect" and new.my_pick_locked and new.my_champion
                    and not old.my_pick_locked and (old.my_champion is not None or old.selection_known)
                    and new.my_champion not in self._acknowledged_picks
                    and old.phase == "ChampSelect"):
                self._acknowledged_picks.add(new.my_champion)
                text = LOCK_REACTIONS[self._lock_reaction_index % len(LOCK_REACTIONS)].format(champion=new.my_champion)
                self._lock_reaction_index += 1
                self._notify("pick-locked", text, 2, 10, champion=new.my_champion)
        self._team_picks_changed(old, new)

    @property
    def session_active(self):
        with self._lock:
            return self._session.active

    def _observe_session(self, **evidence):
        with self._lock:
            changed = self._session.observe(time.monotonic(), self.tracker.epoch, **evidence)
            active = self._session.active
            ready = (active and evidence.get("live") and not evidence.get("ended")
                     and self._session_live_epoch != self.tracker.epoch)
            if ready:
                self._session_live_epoch = self.tracker.epoch
        if changed and self.on_session_active_changed:
            self.on_session_active_changed(active)
        if changed and not active:
            self.journal.finish(self.tracker.epoch, self._events)
            self.lane.reset(self.tracker.epoch)
        if ready and not changed and self.on_live_game_ready:
            self.on_live_game_ready()

    def _team_picks_changed(self, old, new):
        if new.phase != "ChampSelect" or not new.selection_known:
            return
        from core.league_voice import draft_pick_reaction

        # The existing locked-pick lists contain no player identities. Allies
        # include the local player; her own lock already has a separate reaction.
        self._observed_allied_picks.update(old.allied_champions if old.phase == "ChampSelect" else ())
        self._observed_enemy_picks.update(old.enemy_champions if old.phase == "ChampSelect" else ())
        allied = tuple(dict.fromkeys(name for name in new.allied_champions
                       if name not in self._observed_allied_picks and not name.startswith("Champion #")
                       and not (new.my_pick_locked and name == new.my_champion)))
        enemy = tuple(dict.fromkeys(name for name in new.enemy_champions
                      if name not in self._observed_enemy_picks and not name.startswith("Champion #")))
        self._observed_allied_picks.update(new.allied_champions)
        self._observed_enemy_picks.update(new.enemy_champions)
        # Bootstrap and reconnect silently: existing picks are a baseline, not
        # fresh events. Hover lists are deliberately never used here.
        if old.phase != "ChampSelect" or not old.selection_known or not (allied or enemy):
            return
        text = draft_pick_reaction(allied, enemy, self._draft_reaction_index)
        self._draft_reaction_index += 1
        self._notify("draft-picks", text, 1, 10, delay_seconds=.15, allied_picks=allied, enemy_picks=enemy)

    def _notify(self, key, text, priority=1, lifetime=6, champion=None, delay_seconds=0,
                allied_picks=(), enemy_picks=(), detected_ns=0):
        if self.on_notification and config.LEAGUE_PROACTIVE_VOICE_ENABLED:
            from core.league_voice import LeagueAnnouncement
            started = self.metrics.start()
            now = time.monotonic()
            queued = self.metrics.start() or (time.perf_counter_ns() if config.VOICE_LATENCY_METRICS_ENABLED else 0)
            self.on_notification(LeagueAnnouncement(text, key, now, lifetime, priority,
                                                   queued, self.tracker.epoch,
                                                   champion, now + delay_seconds if delay_seconds else 0,
                                                   allied_picks, enemy_picks, detected_ns or queued))
            self.metrics.finish("event_to_callback", started)

    def read(self, cancel_event=None):
        check_cancelled(cancel_event)
        with self._lock:
            now = time.monotonic()
            if self._snapshot is None or now - self._captured_at > max(2, config.LEAGUE_CACHE_SECONDS * 3):
                return None
            return {**copy.deepcopy(self._snapshot), "age_seconds": round(now - self._captured_at, 2)}

    def roster(self):
        with self._lock:
            if self._snapshot is None or time.monotonic() - self._captured_at > max(2, config.LEAGUE_CACHE_SECONDS * 3):
                return ()
            return tuple(dict(item) for item in self._roster)

    def events(self):
        with self._lock:
            age = time.monotonic() - self._events_at
            # A recorded ending remains answerable briefly after the API exits;
            # it is an explicit past-match fact, never a current player snapshot.
            if self.tracker.ended and self._events and age <= 30:
                return self._events
            if not self._events_available or age > max(2, config.LEAGUE_EVENT_POLL_INTERVAL * 4):
                return ()
            return self._events

    def announcement_current(self, announcement):
        with self._lock:
            same_match = announcement.epoch == self.tracker.epoch
        if announcement.event_key.startswith("visual:"):
            return bool(same_match and self.visual_announcement_current and self.visual_announcement_current(announcement))
        if announcement.event_key == "champ-select":
            return self.champ_select.snapshot().phase == "ChampSelect"
        if announcement.event_key in ("pick-hover", "pick-locked"):
            snapshot = self.champ_select.snapshot()
            return (snapshot.phase == "ChampSelect" and bool(announcement.champion)
                    and snapshot.my_champion == announcement.champion
                    and snapshot.my_pick_locked == (announcement.event_key == "pick-locked"))
        if announcement.event_key == "draft-picks":
            snapshot = self.champ_select.snapshot()
            return (snapshot.phase == "ChampSelect" and snapshot.selection_known
                    and bool(announcement.allied_picks or announcement.enemy_picks)
                    and all(name in snapshot.allied_champions for name in announcement.allied_picks)
                    and all(name in snapshot.enemy_champions for name in announcement.enemy_picks))
        return same_match and bool(self.events())

    @staticmethod
    async def _get(client, endpoint):
        if endpoint not in ("eventdata", "activeplayer", "playerlist", "gamestats"):
            raise ValueError("Unsupported League endpoint")
        response = await client.get(BASE_URL + endpoint)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("Oversized League response")
        return response.json()

    async def refresh_stats(self, client):
        # Concurrent refresh callers coalesce to one in-flight request sequence.
        if self._stats_lock is None:
            self._stats_lock = asyncio.Lock()
        requested = time.monotonic()
        async with self._stats_lock:
            if self._stats_completed_at >= requested:
                return
            try:
                game = await self._get(client, "gamestats")
                if not isinstance(game, dict) or not _numbers(game, ("gameTime",)):
                    raise ValueError("Missing game time")
                with self._lock:
                    epoch = self.tracker.epoch
                    self.tracker.set_game_time(game.get("gameTime"))
                    if self.tracker.epoch != epoch:
                        self.journal.reset(self.tracker.epoch)
                        self._roster = ()
                        self._events = ()
                        self._events_available = False
                        self._actors = {}
                        self._own_aliases = frozenset()
                        self._snapshot = None
                    epoch = self.tracker.epoch
                    if self.tracker.ended:
                        self._snapshot = None
                        # Event ingestion owns the ending transition.
                        return
                active = await self._get(client, "activeplayer")
                players = await self._get(client, "playerlist")
                snapshot = own_player_snapshot(active, players, game)
                if snapshot is None or not isinstance(players, list):
                    raise ValueError("Missing active player")
                identity = active.get("riotId") or active.get("summonerName")
                own = next((player for player in players if isinstance(player, dict) and identity
                            and identity in (player.get("riotId"), player.get("summonerName"))), {})
                team = own.get("team")
                aliases = frozenset(value for key in ("riotId", "summonerName", "riotIdGameName")
                                    for value in (active.get(key), own.get(key))
                                    if isinstance(value, str) and value and sum(
                                        value in (player.get("riotId"), player.get("summonerName"), player.get("riotIdGameName"))
                                        for player in players if isinstance(player, dict)) == 1)
                actors = {}
                from core.league_events import safe_text
                for player in players[:20]:
                    if not isinstance(player, dict):
                        continue
                    side = "" if not team or not player.get("team") else ("ally" if player["team"] == team else "enemy")
                    for key in ("riotId", "summonerName", "riotIdGameName"):
                        if isinstance(player.get(key), str):
                            actors[player[key]] = (safe_text(player.get("championName")), side)
                if self.tracker.epoch == epoch:
                    relabelled = self.tracker.update_actors(actors)
                else:
                    relabelled = False
                with self._lock:
                    visual_update = None
                    if not self.tracker.ended and self.tracker.epoch == epoch:
                        self._snapshot, self._captured_at = snapshot, time.monotonic()
                        # Public champion/team/assigned-position metadata only.
                        self._roster = tuple({"champion": safe_text(player.get("championName")),
                                              "side": "ally" if player.get("team") == team else "enemy",
                                              "position": safe_text(player.get("position"), 20).upper()}
                                             for player in players[:20] if isinstance(player, dict) and team and player.get("team"))
                        visual_update=(epoch,game.get("gameMode"),game.get("mapNumber"),
                                       safe_text(own.get("position"),20).upper(),team,safe_text(own.get("championName")))
                        self.journal.update(epoch, snapshot)
                        self._actors = actors
                        self._own_aliases = aliases
                        if relabelled:
                            self._events = tuple(self.tracker.history)
                if visual_update:
                    self.lane.update(*visual_update)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                with self._lock:
                    self._snapshot = None
            finally:
                self._stats_completed_at = time.monotonic()
                self._observe_session(live=self._snapshot is not None, ended=self.tracker.ended)

    async def poll_events(self, client):
        started = self.metrics.start()
        try:
            data = await self._get(client, "eventdata")
            now = time.monotonic()
            # Event parsing happens outside the publication lock. This producer
            # owns tracker mutation; readers only see the published tuple.
            epoch = self.tracker.epoch
            fresh = self.tracker.ingest(data, now, self._actors)
            detected = self.metrics.start() or (time.perf_counter_ns() if config.VOICE_LATENCY_METRICS_ENABLED else 0)
            with self._lock:
                if self.tracker.epoch != epoch:
                    self.journal.reset(self.tracker.epoch)
                    self._roster = ()
                    self._snapshot = None
                    self._actors = {}
                    self._own_aliases = frozenset()
                if not self._events or fresh or self.tracker.epoch != epoch:
                    self._events = tuple(self.tracker.history)
                self._events_available, self._events_at = True, now
                self.journal.observe_events(self.tracker.epoch,self._events,fresh)
                if self.tracker.ended:
                    self.journal.finish(self.tracker.epoch, self._events)
                    self._snapshot = None
            self._observe_session(live=not self.tracker.ended, ended=self.tracker.ended)
            self.metrics.finish("event_http_to_identified", started)
            for event in fresh:
                # Sparse acknowledgments, never automatic tactical coaching.
                if event.name == "GameEnd":
                    self._notify("game-end", event.describe(), 3, 10, detected_ns=detected)
                elif event.name in ("DragonKill", "BaronKill", "HeraldKill", "TurretKilled", "InhibKilled"):
                    identity = event.event_id if event.event_id is not None else event.game_time
                    self._notify(f"objective:{event.name}:{identity}", event.describe(), 2, 8, detected_ns=detected)
                elif event.name == "ChampionKill":
                    identity = event.event_id if event.event_id is not None else event.game_time
                    if event.raw_victim in self._own_aliases:
                        self._notify(f"own-death:{identity}", "Your champion went down, chum.", 1, 10, detected_ns=detected)
                    elif event.raw_killer in self._own_aliases:
                        self._notify(f"own-kill:{identity}", "You got a kill! Nice.", 1, 10, detected_ns=detected)
            # Includes publication and selected callback dispatch, excludes JSON
            # parsing. Polls with no newly identified events aren't samples.
            if fresh:
                self.metrics.finish("event_identified_to_dispatch", detected)
            return True
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            with self._lock:
                self._events_available = False
                self._snapshot = None
            self._observe_session()
            return False

    async def _event_loop(self, client):
        while True:
            started = time.monotonic()
            ok = await self.poll_events(client)
            interval = max(.1, config.LEAGUE_EVENT_POLL_INTERVAL) if ok else 2.0
            await asyncio.sleep(max(.01, interval - (time.monotonic() - started)))

    async def _stats_loop(self, client):
        while True:
            await self.refresh_stats(client)
            await asyncio.sleep(max(.5, config.LEAGUE_CACHE_SECONDS) if self._snapshot else 2)

    async def run(self):
        try:
            async with httpx.AsyncClient(verify=local_tls(), trust_env=False, follow_redirects=False,
                                         timeout=httpx.Timeout(config.LEAGUE_REQUEST_TIMEOUT_SECONDS),
                                         limits=httpx.Limits(max_connections=2, max_keepalive_connections=2,
                                                             keepalive_expiry=30)) as client:
                tasks = []
                if config.LEAGUE_EVENTS_ENABLED:
                    tasks.append(asyncio.create_task(self._event_loop(client)))
                if config.LEAGUE_LIVE_DATA_ENABLED:
                    tasks.append(asyncio.create_task(self._stats_loop(client)))
                if config.LEAGUE_CHAMP_SELECT_ENABLED:
                    tasks.append(asyncio.create_task(self.champ_select.run()))
                try:
                    if tasks:
                        await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            with self._lock:
                self._snapshot = None
                self._events_available = False
                self._events = ()
                self._actors = {}
                self._own_aliases = frozenset()
                self.tracker.reset()
                self.journal.reset(self.tracker.epoch)
                self._roster = ()
            self.knowledge.close()
            self.champ_select.update(phase="Unavailable", clear=True)
            self._observe_session(shutdown=True)

    def answer(self, text, verified_only=False):
        started = self.metrics.start()
        try:
            from core.league_answers import league_answer
            return league_answer(text, self, verified_only=verified_only)
        finally:
            self.metrics.finish("question_to_direct_answer", started)

    def companion_answer(self, text, cancel_event=None):
        """New cache/reference routes; no screen work or automatic model calls."""
        with self._lock:
            before = self.tracker.epoch
        check_cancelled(cancel_event)
        game = self.read(cancel_event) if self.stats_enabled else None
        with self._lock:
            epoch = self.tracker.epoch
        if epoch != before:
            game = None
        self.journal.reset(epoch)
        if self.journal.coaching_request(text) and self.session_active:
            return "Let's review the completed match after this active session. I won't run post-game analysis during play."
        if self.postgame_context(text):
            return None  # Explicit reasoning request uses the existing streamed conversation route.
        note = self.journal.sighting_request(text, game, epoch, self.champ_select.names.names.values())
        if note:
            return note
        postgame = self.journal.postgame_answer(text)
        if postgame:
            return postgame
        replay = self.journal.replay_answer(text)
        if replay:
            return replay
        if re.search(r"\bwhere.{0,30}(enemy|jungler)|predict.{0,20}(route|path)", text, re.I):
            return "I can't verify current enemy positions or predict jungle routes. A reported or recorded last sighting is only a past observation."
        if re.fullmatch(r"\s*(?:what (?:items? (?:have i (?:bought|got)|do i have))|what(?:'s| is) my (?:current )?inventory)[?.! ]*", text, re.I):
            return ("Your API-reported current inventory: " + (", ".join(game.get("items", [])) or "no named items") +
                    ". This doesn't include sold items.") if game else "I don't have a fresh inventory snapshot, so I can't verify your items."
        if re.search(r"\bwhat happened in (?:the )?last team ?fight\b", text, re.I):
            events = self.events()
            kills = [event for event in events if event.name == "ChampionKill"]
            return ("I can't identify teamfight boundaries from this API. Last recorded kills: " +
                    " ".join(f"{int(event.game_time)//60}:{int(event.game_time)%60:02d}, {event.describe()}" for event in kills[-5:])) if kills else "I don't have enough recorded kills to describe a teamfight, and I won't invent one."
        if re.search(r"\bwhich objectives (?:were|have been) recorded\b", text, re.I):
            events = [event for event in self.events() if event.name in ("DragonKill", "BaronKill", "HeraldKill", "HordeKill", "TurretKilled", "InhibKilled")]
            return "Recorded objectives: " + " ".join(f"{int(event.game_time)//60}:{int(event.game_time)%60:02d}, {event.describe()}" for event in events[-5:]) if events else "No objectives are available in my current bounded event history."
        if re.search(r"\bcurrent kda\b", text, re.I):
            return direct_league_answer("what is my kda", game) or "I don't have a fresh KDA snapshot."
        own_reference = re.search(r"\bmy (?:league )?(?:champion|ability|abilities|[qwer])\b",text,re.I)
        hint = game.get("champion") if game and own_reference else None
        if own_reference and not hint and not self.knowledge.champions_in(text):
            return None  # Preserve verified API -> screen fallback for an unresolved personal reference.
        if re.search(r"enemy jungler", text, re.I):
            junglers = [entry["champion"] for entry in self.roster() if entry["side"] == "enemy" and entry["position"] == "JUNGLE"]
            if len(junglers) != 1:
                return "The public API hasn't identified a unique enemy jungle role. Tell me the champion's name; I won't infer it from their kit."
            hint = junglers[0]
        return self.knowledge.answer(text, hint, cancel_event,
                                     item_ids=game.get("item_ids",()) if game else (),
                                     map_id=game.get("game",{}).get("mapNumber") if game else None)

    def postgame_context(self, text):
        return None if self.session_active else self.journal.reasoning_context(text)

    def context_for(self, text):
        review = self.postgame_context(text)
        if review:
            return {"post_game_review":review}
        if not league_question(text, champion_names=self.champ_select.names.names.values()):
            return None
        result = {"own_player": self.read() if self.stats_enabled else None}
        champion = self.champ_select.snapshot()
        if self.champ_select_enabled and champion.phase == "ChampSelect" and champion.connected:
            result["champ_select"] = champion.summary()
        events = self.events() if self.events_enabled else ()
        if events:
            # Only the recent relevant facts, never the raw event history.
            result["recent_events"] = [{"game_time": event.game_time, "fact": event.describe()}
                                       for event in events[-3:]]
            result["match_ended"] = any(event.name == "GameEnd" for event in events)
        reference = self.knowledge.context_for(text)
        if reference:
            result["static_reference"] = reference
        return result if any(result.values()) else None


def direct_league_answer(text, game):
    if not game:
        return None
    text = text.casefold().replace("\u2019", "'").strip(" .?!")
    stats, scores = game.get("stats", {}), game.get("scores", {})
    if re.fullmatch(r"(?:what(?:'s| is) my (?:health|hp)|how much (?:health|hp) do i have)(?: right now)?", text):
        if "currentHealth" in stats and stats.get("maxHealth", 0) > 0:
            return f"You have {round(stats['currentHealth'])} of {round(stats['maxHealth'])} health, chum."
    if re.fullmatch(r"(?:what(?:'s| is) my gold|how much gold do i have)(?: right now)?", text) and "currentGold" in game:
        return f"You've got {int(game['currentGold'])} gold."
    if re.fullmatch(r"what(?:'s| is) my (?:level|current level)|what level am i", text) and "level" in game:
        return f"You're level {int(game['level'])}."
    if re.fullmatch(r"what(?:'s| is) my (?:kda|score)", text) and all(key in scores for key in ("kills", "deaths", "assists")):
        return ", ".join(f"{int(scores[key])} {noun}{'' if int(scores[key]) == 1 else 's'}"
                         for key, noun in (("kills", "kill"), ("deaths", "death"), ("assists", "assist"))) + "."
    if re.fullmatch(r"what(?:'s| is) my (?:cs|creep score)", text) and "creepScore" in scores:
        return f"You're at {int(scores['creepScore'])} CS."
    if re.fullmatch(r"who am i playing|what champion am i playing", text) and game.get("champion"):
        return f"You're playing {game['champion']}, chum."
    return None


def health_advice(game, source_id, captured_at):
    from core.proactivity import ScreenAdvice
    stats = game.get("stats", {})
    health, maximum = stats.get("currentHealth", 0), stats.get("maxHealth", 0)
    if maximum > 0 and 0 < health < maximum * 0.2 and game.get("isDead") is False:
        return ScreenAdvice("Your health is under 20 percent. Check for a safe retreat, chum.",
                            "league-low-health", f"Health {round(health)}/{round(maximum)}",
                            source_id, captured_at)
    return None

"""Immutable, bounded Live Client event tracking; no network or game inference."""

import math
from collections import deque
from dataclasses import dataclass, field, replace


def safe_text(value, limit=80):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


@dataclass(frozen=True)
class MatchEvent:
    event_id: int | None
    name: str
    game_time: float
    received_at: float
    killer: str = ""
    victim: str = ""
    detail: str = ""
    side: str = ""  # Only from matching the exposed KillerName to a known team.
    result: str = ""
    raw_killer: str = field(default="", repr=False)
    raw_victim: str = field(default="", repr=False)
    fingerprint: tuple = field(default=(), repr=False)

    def describe(self):
        actor = self.killer or "A player"
        side = {"ally": "Your team", "enemy": "The enemy team"}.get(self.side, actor)
        if self.name == "GameStart":
            return "The match started."
        if self.name == "GameEnd":
            return {"Win": "The match ended in a victory!", "Lose": "The match ended in a defeat."}.get(
                self.result, "The match ended; the API hasn't supplied your result.")
        if self.name == "ChampionKill":
            return f"{actor} killed {self.victim or 'a champion'}."
        objective = {"DragonKill": (self.detail + " Dragon").strip(), "BaronKill": "Baron",
                     "HeraldKill": "Rift Herald", "HordeKill": "Void Grubs",
                     "TurretKilled": "a turret", "InhibKilled": "an inhibitor"}.get(self.name)
        if objective:
            return f"{side} took {objective}."
        return {"Ace": "An ace was recorded.", "FirstBlood": "First blood was recorded.",
                "FirstBrick": "The first turret fell.", "MinionsSpawning": "Minions spawned.",
                "Multikill": f"{actor} scored a multikill.",
                "InhibRespawned": "An inhibitor respawned."}.get(self.name, f"{self.name} was recorded.")


class EventTracker:
    """Called by one producer. Readers get tuples through LeagueClient's lock.

    EventID high-water marks survive outages and history eviction. Events without
    IDs use a timestamp watermark and fingerprints at that timestamp, so replaying
    a full history cannot reannounce an evicted event. A new match is baselined.
    """
    def __init__(self, limit=64):
        self.history = deque(maxlen=max(1, min(512, int(limit))))
        self.reset()

    def reset(self):
        self.history.clear()
        self.baselined = False
        self.high_id = -1
        self.high_time = -1.0
        self.at_watermark = set()
        self.game_time = None
        self.ended = False
        self.epoch = getattr(self, "epoch", 0) + 1

    def set_game_time(self, game_time):
        if type(game_time) not in (int, float) or not math.isfinite(game_time) or game_time < 0:
            return
        if self.game_time is not None and game_time + 2 < self.game_time:
            self.reset()
        self.game_time = game_time

    def ingest(self, payload, received_at, actors=None):
        raw = payload.get("Events") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise ValueError("Malformed League event response")
        if len(raw) > 20000:
            raise ValueError("Oversized League event history")
        maximum_id = max((item["EventID"] for item in raw if isinstance(item, dict)
                          and type(item.get("EventID")) is int and item["EventID"] >= 0), default=-1)
        if maximum_id >= 0 and self.high_id >= 0 and maximum_id < self.high_id:
            self.reset()
            actors = {}  # Never carry last match's team attribution across a reset.
        parsed = []
        skipped_valid = False
        actors = actors or {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name, when = item.get("EventName"), item.get("EventTime")
            if not isinstance(name, str) or not name or len(name) > 64 or not name.isidentifier():
                continue
            if type(when) not in (int, float) or not math.isfinite(when) or when < 0:
                continue
            event_id = item.get("EventID")
            event_id = event_id if type(event_id) is int and event_id >= 0 else None
            if event_id is not None and event_id <= self.high_id:
                skipped_valid = True
                continue  # Most polls: no dataclasses, labels or signatures allocated.
            if event_id is None and when < self.high_time and not self.ended:
                skipped_valid = True
                continue
            killer = safe_text(item.get("KillerName"))
            victim = safe_text(item.get("VictimName"))
            actor, side = actors.get(killer, (killer, ""))
            victim = actors.get(victim, (victim, ""))[0]
            signature = tuple(safe_text(str(item.get(key, ""))) for key in (
                "KillerName", "VictimName", "DragonType", "TurretKilled", "InhibKilled", "KillStreak",
                "Acer", "AcingTeam", "Result", "Stolen"))
            parsed.append(MatchEvent(event_id, name, float(when), received_at,
                                     actor, victim, safe_text(item.get("DragonType")), side,
                                     item.get("Result") if item.get("Result") in ("Win", "Lose") else "",
                                     killer, safe_text(item.get("VictimName")), signature))
        if raw and not parsed and not skipped_valid:
            raise ValueError("No valid League events")
        parsed.sort(key=lambda event: (event.game_time, event.event_id if event.event_id is not None else -1))
        if parsed and self.ended and parsed[-1].game_time < self.high_time:
            self.reset()
            parsed = [replace(event, killer=event.raw_killer, victim=event.raw_victim, side="") for event in parsed]
        silent = not self.baselined
        fresh = []
        previous_id = self.high_id
        batch_ids = set()
        for event in parsed:
            fingerprint = (event.name, event.fingerprint)
            if event.event_id is not None:
                if event.event_id <= previous_id or event.event_id in batch_ids:
                    continue
                batch_ids.add(event.event_id)
                self.high_id = max(self.high_id, event.event_id)
            else:
                if event.game_time < self.high_time or (event.game_time == self.high_time
                                                       and (fingerprint in self.at_watermark or len(self.at_watermark) >= 1024)):
                    continue
            if event.game_time > self.high_time:
                self.high_time = event.game_time
                self.at_watermark.clear()
            if event.game_time == self.high_time:
                if len(self.at_watermark) < 1024:
                    self.at_watermark.add(fingerprint)
            self.history.append(event)
            self.ended |= event.name == "GameEnd"
            if not silent:
                fresh.append(event)
        self.baselined = True
        return tuple(fresh)

    def update_actors(self, actors):
        # Stats may arrive after the first event poll. Attribute old visible
        # events using actual player metadata without replaying any callbacks.
        changed = False
        for index, event in enumerate(self.history):
            killer, side = actors.get(event.raw_killer, (event.killer, event.side))
            victim = actors.get(event.raw_victim, (event.victim, ""))[0]
            if (killer, side, victim) != (event.killer, event.side, event.victim):
                self.history[index] = replace(event, killer=killer, side=side, victim=victim)
                changed = True
        return changed

"""Bounded reported observations and one post-game record. No location inference."""
import copy
import re
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock

import config


def stamp(seconds):
    return f"{int(seconds)//60:02d}:{int(seconds)%60:02d}"


@dataclass(frozen=True)
class ReportedSighting:
    champion: str
    location: str
    game_time: float
    received_at: float
    source: str = "user-reported; not verified by API or vision"


class MatchJournal:
    def __init__(self):
        self._lock = Lock()
        self.epoch = None
        self.notes = deque(maxlen=64)
        self.last_game = None
        self.archive = None
        self.replay = None
        self.objectives = deque(maxlen=128)
        self._events_seeded = False

    def reset(self, epoch):
        with self._lock:
            if self.epoch is None or epoch > self.epoch:
                self.epoch = epoch
                self.notes.clear()
                self.last_game = None
                self.objectives.clear()
                self._events_seeded = False

    def observe_events(self, epoch, history, fresh):
        self.reset(epoch)
        with self._lock:
            if epoch != self.epoch:
                return
            source = fresh if self._events_seeded else history
            self._events_seeded = True
            for event in source:
                if event.name in ("DragonKill","BaronKill","HeraldKill","HordeKill","TurretKilled","InhibKilled"):
                    if event not in self.objectives:
                        self.objectives.append(event)

    def update(self, epoch, game):
        self.reset(epoch)
        with self._lock:
            if epoch == self.epoch:
                self.last_game = game  # Producer-owned immutable-by-convention snapshot.

    def finish(self, epoch, events):
        with self._lock:
            if epoch == self.epoch and self.last_game is not None:
                by_id = {("id",event.event_id) if event.event_id is not None else (event.name,event.game_time,event.fingerprint):event
                         for event in (*self.objectives,*events)}
                retained = tuple(sorted(by_id.values(),key=lambda event:event.game_time))
                self.archive = {"epoch": epoch, "own_player": copy.deepcopy(self.last_game),
                                "events": retained, "reported_sightings": tuple(self.notes),
                                "history_bounded": True}
                self.notes.clear()
                self.last_game = None

    def sighting_request(self, text, game, epoch, known_names):
        match = re.fullmatch(r"\s*(?:capri[, ]+)?(?:i (?:just )?(?:saw|spotted)|(?:note|remember) (?:that )?i (?:saw|spotted)) "
                             r"(.+?) (?:at |near |in |on )?(top|mid|middle|bot|bottom|river|jungle)(?: lane)?[.! ]*", text, re.I)
        query = re.search(r"\b(?:what|when|where).*(?:report|sighting|last (?:saw|spotted|seen))", text, re.I)
        if not match and not query:
            return None
        if not config.LEAGUE_SIGHTING_NOTES_ENABLED:
            return "User-reported sighting notes are disabled pending the product's Riot review. I won't track enemy locations."
        if match:
            name = next((name for name in known_names if name.casefold() == match[1].strip().casefold()), None)
            if not game or name is None or "gameTime" not in game.get("game", {}):
                return "I need a fresh match clock and an unambiguous champion name to record that report."
            when = game["game"]["gameTime"]
            location = {"middle": "mid", "bottom": "bot"}.get(match[2].casefold(), match[2].casefold())
            note = ReportedSighting(name, location, when, time.monotonic())
            with self._lock:
                if self.epoch != epoch:
                    return "The match changed before I could save that report."
                self.notes.append(note)
            return f"Noted: you reported {name} {location} at {stamp(when)}. That isn't a verified current position."
        with self._lock:
            candidates = [note for note in self.notes if note.champion.casefold() in text.casefold()]
            note = candidates[-1] if candidates else self.notes[-1] if self.notes else None
        if note is None:
            return "I don't have a user-reported sighting for this match."
        age = max(0, int(time.monotonic() - note.received_at))
        return (f"You reported {note.champion} {note.location} at {stamp(note.game_time)}, about {age} seconds ago. "
                "That was your report, not their current location.")

    def postgame_answer(self, text):
        if not re.search(r"\b(post.?game|post.?match|last match|last game|review (?:my|the) (?:match|game)|objective chronology)\b", text, re.I):
            return None
        with self._lock:
            archive = self.archive
        if archive is None:
            return "I don't have a completed match record in this Capri session yet."
        objectives = [event for event in archive["events"] if event.name in (
            "DragonKill", "BaronKill", "HeraldKill", "HordeKill", "TurretKilled", "InhibKilled")]
        ending = next((event for event in reversed(archive["events"]) if event.name == "GameEnd"), None)
        pieces = [ending.describe() if ending else "League became inactive or unavailable; the API didn't supply a match result."]
        if objectives:
            pieces.append("Recorded objectives: " + " ".join(f"{stamp(event.game_time)}: {event.describe()}" for event in objectives[-5:]))
        else:
            pieces.append("No objectives remain in my bounded event record.")
        pieces.append("These events don't show positioning or prove a mistake. A reviewed player-view recording can supply that context.")
        return " ".join(pieces)

    def publish_replay(self, report):
        with self._lock:
            self.replay = copy.deepcopy(report)

    def replay_answer(self, text):
        if re.match(r"\s*(?:please )?(?:analy[sz]e|load|review)\b",text,re.I):
            return None
        if not re.search(r"\b(replay|recording|minimap timeline|missed sightings)\b", text, re.I):
            return None
        with self._lock:
            report = self.replay
        if report is None:
            return "No recorded-game analysis is loaded. Provide a player-view video and minimap calibration after the match."
        confirmed = report.get("confirmed_sightings", [])
        if not confirmed:
            return f"The recording has {len(report.get('candidates', []))} candidate sightings, but none are human-confirmed. Review the evidence thumbnails first."
        selected = [item for item in confirmed if item.get("player_marked_missed") is True] if re.search("missed",text,re.I) else confirmed
        if not selected:
            return "You haven't marked a confirmed sighting as missed. I can't infer what you noticed or where you were looking."
        pieces = [f"Video {stamp(item['video_time_s'])}: {item['champion']} icon in {item['region']}."
                  for item in selected[:6]]
        missed = sum(item.get("player_marked_missed") is True for item in confirmed)
        return " ".join(pieces) + f" You marked {missed} as missed. I can't infer attention, routes or positions between these sightings."

    @staticmethod
    def coaching_request(text):
        return bool(re.search(r"\b(detail\w*|coach\w*|improv\w*|mistakes?|explain)\b",text,re.I)
                    and re.search(r"\b(post.?game|post.?match|last (?:match|game)|recording|replay)\b",text,re.I))

    def reasoning_context(self, text):
        if not self.coaching_request(text):
            return None
        with self._lock:
            archive, replay = self.archive, self.replay
        if archive is None and not (replay or {}).get("confirmed_sightings"):
            return None
        evidence = {"limits":["Separate recorded observations from interpretations and uncertainty.",
                               "No inferred enemy routes/current positions, fight safety or player attention.",
                               "Video timestamps and API game-clock timestamps are different clocks.",
                               "The event log is bounded; it cannot establish positioning mistakes."]}
        if archive:
            evidence["last_recorded_own_snapshot"] = {key:archive["own_player"].get(key) for key in ("champion","scores")}
            evidence["game_clock_events"] = [{"at":event.game_time,"fact":event.describe()} for event in archive["events"][-8:]]
            evidence["user_reported_notes"] = [{"champion":note.champion,"location":note.location,"at":note.game_time,
                                                "source":note.source} for note in archive["reported_sightings"][-4:]]
        if replay:
            evidence["user_reviewed_video_sightings"] = [{key:item.get(key) for key in (
                "video_time_s","champion","region","player_marked_missed","review")}
                for item in replay.get("confirmed_sightings",[])[:8]]
        return evidence

"""Read-only own-player context from Riot's local Live Client Data API."""

import math
import re
import time
from threading import Lock

import httpx

import config
from core.cancellation import check_cancelled

BASE_URL = "https://127.0.0.1:2999/liveclientdata/"


def league_question(text, recent=()):
    if re.search(r"\b(league(?: of legends)?|lol|champion|summoner|jungl\w*|gank|baron|"
                 r"dragon|cs|kda|score|gold|hp|mana|health|level|lane|farming|build|items|who am i playing)\b", text, re.I):
        return True
    return bool(re.search(r"\b(what should i do|fight|retreat|recall|play|buy|push|wave|matchup)\b", text, re.I)
                and any(re.search(r"\b(league|champion|lol|lane)\b", item.get("content", ""), re.I)
                        for item in recent[-6:]))


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
              "game": _numbers(game, ("gameTime",)),
              "scores": _numbers(own.get("scores", {}), ("kills", "deaths", "assists", "creepScore", "wardScore"))}
    if isinstance(own.get("championName"), str):
        result["champion"] = own["championName"][:80]
    result["items"] = [item["displayName"][:100] for item in (own.get("items") or [])
                       if isinstance(item, dict) and isinstance(item.get("displayName"), str)][:8]
    if type(own.get("isDead")) is bool:
        result["isDead"] = own["isDead"]
    return result


class LeagueClient:
    def __init__(self):
        self._lock = Lock()
        self._snapshot = None
        self._captured_at = 0.0
        self._retry_at = 0.0

    def read(self, cancel_event=None):
        check_cancelled(cancel_event)
        with self._lock:
            now = time.monotonic()
            if now < self._retry_at:
                return None
            if self._snapshot and now - self._captured_at <= config.LEAGUE_CACHE_SECONDS:
                return {**self._snapshot, "age_seconds": round(now - self._captured_at, 2)}
            self._snapshot = None  # Never reuse an earlier match after a failed refresh.
            try:
                # Riot documents a self-signed certificate on this fixed loopback
                # endpoint. No credentials, external hosts or redirects are used.
                with httpx.Client(verify=False, trust_env=False, follow_redirects=False,
                                  timeout=config.LEAGUE_REQUEST_TIMEOUT_SECONDS) as client:
                    def get(endpoint):
                        check_cancelled(cancel_event)
                        response = client.get(BASE_URL + endpoint)
                        response.raise_for_status()
                        check_cancelled(cancel_event)
                        if len(response.content) > 2_000_000:
                            raise ValueError("Oversized League response")
                        return response.json()
                    active = get("activeplayer")
                    captured_at = time.monotonic()
                    players = get("playerlist")
                    game = get("gamestats")
                snapshot = own_player_snapshot(active, players, game)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                snapshot = None
            check_cancelled(cancel_event)
            if snapshot is None:
                self._retry_at = time.monotonic() + 5
                return None
            self._snapshot = snapshot
            self._captured_at = captured_at
            return {**snapshot, "age_seconds": round(time.monotonic() - captured_at, 2)}


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

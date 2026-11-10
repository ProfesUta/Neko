"""Single-slot event coalescing and freshness; speech uses Capri's existing workers."""

import time
from collections import deque
from dataclasses import dataclass, replace


HOVER_REACTIONS = (
    "Hmm... {champion}? That's the plan?",
    "Oh, {champion}. Feeling adventurous today?",
    "{champion}, huh? You're keeping me on my toes.",
    "Really... {champion}? All right, I'm listening.",
)
LOCK_REACTIONS = (
    "Okay... {champion}. Your call.",
    "All right... {champion} it is.",
    "Sure... {champion}. Let's see how this goes.",
    "Fine... {champion}. I'll save my commentary.",
)
ALLY_PICK_REACTIONS = (
    "Our team locked {champions}. Okay, I see the vision.",
    "Oh, {champions} on our side. All right, I'm watching.",
    "{champions} for our team. Let's see how this goes.",
)
ENEMY_PICK_REACTIONS = (
    "They locked {champions}. Hmm... interesting.",
    "Oh, {champions} on the other side. Got it.",
    "{champions} for the enemy team. Of course.",
)


def draft_pick_reaction(allied, enemy, variant=0):
    def names(champions):
        return champions[0] if len(champions) == 1 else ", ".join(champions[:-1]) + " and " + champions[-1]
    return " ".join(templates[variant % len(templates)].format(champions=names(champions))
                    for templates, champions in ((ALLY_PICK_REACTIONS, allied), (ENEMY_PICK_REACTIONS, enemy))
                    if champions)


@dataclass(frozen=True)
class LeagueAnnouncement:
    text: str
    event_key: str
    captured_at: float
    lifetime: float = 6
    priority: int = 1
    queued_ns: int = 0
    epoch: int = 0
    champion: str | None = None
    not_before: float = 0.0
    allied_picks: tuple = ()
    enemy_picks: tuple = ()

    def is_fresh(self):
        return 0 <= time.monotonic() - self.captured_at <= self.lifetime


class LeagueVoicePolicy:
    def __init__(self, cooldown=5):
        self.pending = None
        self.cooldown = cooldown
        self.last_started = float("-inf")
        self._last_event_key = None
        self._offered = deque(maxlen=32)

    def offer(self, announcement):
        if not announcement.is_fresh():
            return False
        key = (announcement.event_key, announcement.text, announcement.epoch)
        if key in self._offered:
            return False
        if (announcement.event_key == "draft-picks" and self.pending is not None
                and self.pending.event_key == "draft-picks" and self.pending.is_fresh()
                and self.pending.epoch == announcement.epoch):
            # One slot, one short reaction for picks arriving while Capri is busy.
            # Keep the oldest deadline so newer picks cannot keep old ones alive.
            allied = tuple(dict.fromkeys(self.pending.allied_picks + announcement.allied_picks))
            enemy = tuple(dict.fromkeys(self.pending.enemy_picks + announcement.enemy_picks))
            self.pending = replace(self.pending, text=draft_pick_reaction(allied, enemy),
                                   allied_picks=allied, enemy_picks=enemy,
                                   not_before=max(self.pending.not_before, announcement.not_before))
            return True
        if (self.pending is not None and self.pending.is_fresh()
                and self.pending.event_key == "draft-picks" and announcement.event_key == "pick-hover"):
            return False  # A completed team pick matters more than another hover.
        if self.pending is None or not self.pending.is_fresh() or announcement.priority >= self.pending.priority:
            self.pending = announcement
            return True
        return False

    def take(self, available):
        if self.pending is not None and not self.pending.is_fresh():
            self.pending = None
        ready_at = self.next_ready_at()
        if not available or ready_at is None or time.monotonic() < ready_at:
            return None
        result, self.pending = self.pending, None
        self.last_started = time.monotonic()
        self._last_event_key = result.event_key
        self._offered.append((result.event_key, result.text, result.epoch))
        return result

    def next_ready_at(self):
        if self.pending is None:
            return None
        # One verified lock transition should follow the hover naturally, rather
        # than sitting behind the cooldown intended for repeated idle comments.
        gap = 0 if self.pending.event_key in ("pick-locked", "draft-picks") or (
            self.pending.event_key == "pick-hover" and self._last_event_key == "champ-select") else self.cooldown
        return max(self.pending.not_before, self.last_started + gap)

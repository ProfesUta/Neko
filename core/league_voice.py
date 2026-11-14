"""Single-slot event coalescing and freshness; speech uses Capri's existing workers."""

import time
from collections import deque
from dataclasses import dataclass, replace, field


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
    detected_ns: int = 0
    cancel_event: object = field(default=None,compare=False,repr=False)
    first_raw_ns: int = field(default=0,compare=False)
    confirmed_ns: int = field(default=0,compare=False)
    accepted_ns: int = field(default=0,compare=False)

    def is_fresh(self,now=None):
        now=time.monotonic() if now is None else now
        return ((self.cancel_event is None or not self.cancel_event.is_set())
                and 0 <= now - self.captured_at <= self.lifetime)


class LeagueVoicePolicy:
    def __init__(self, cooldown=5,on_status=None,clock=None):
        self.pending = None
        self.cooldown = cooldown
        self.last_started = float("-inf")
        self._last_event_key = None
        self._offered = deque(maxlen=32)
        self.on_status=on_status
        self.clock=clock or (lambda:time.monotonic())
        self.inflight=None

    def _notify(self,item,state):
        if self.on_status:self.on_status(item,state)

    def clear(self):
        self.discard_pending()
        if self.inflight:self._notify(self.inflight,"dropped")
        self.pending=self.inflight=None

    def discard_pending(self):
        if self.pending:self._notify(self.pending,"dropped")
        self.pending=None

    def started(self,item):
        self.last_started=self.clock();self._last_event_key=item.event_key
        key=(item.event_key,item.text,item.epoch)
        if key not in self._offered:self._offered.append(key)
        self._notify(item,"started")

    def finish(self,item):
        if self.inflight==item:
            if (item.event_key,item.text,item.epoch) not in self._offered:self._notify(item,"dropped")
            self.inflight=None

    def offer(self, announcement):
        if not announcement.is_fresh(self.clock()):
            self._notify(announcement,"dropped")
            return False
        key = (announcement.event_key, announcement.text, announcement.epoch)
        if key in self._offered or announcement==self.pending or announcement==self.inflight:
            return False
        # Do not accept a one-second visual alert into a known five-second
        # speech cooldown. Return the real earliest opportunity, so the visual
        # producer retries fresh evidence then instead of churning proposals.
        ready=max(announcement.not_before,self.last_started+self.cooldown)
        if announcement.event_key.startswith("visual:") and ready>announcement.captured_at+announcement.lifetime:
            self._notify(replace(announcement,not_before=ready),"dropped")
            return False
        if (announcement.event_key == "draft-picks" and self.pending is not None
                and self.pending.event_key == "draft-picks" and self.pending.is_fresh(self.clock())
                and self.pending.epoch == announcement.epoch):
            # One slot, one short reaction for picks arriving while Capri is busy.
            # Keep the oldest deadline so newer picks cannot keep old ones alive.
            allied = tuple(dict.fromkeys(self.pending.allied_picks + announcement.allied_picks))
            enemy = tuple(dict.fromkeys(self.pending.enemy_picks + announcement.enemy_picks))
            self.pending = replace(self.pending, text=draft_pick_reaction(allied, enemy),
                                   allied_picks=allied, enemy_picks=enemy,
                                   not_before=max(self.pending.not_before, announcement.not_before))
            return True
        if (self.pending is not None and self.pending.is_fresh(self.clock())
                and self.pending.event_key == "draft-picks" and announcement.event_key == "pick-hover"):
            self._notify(announcement,"dropped")
            return False  # A completed team pick matters more than another hover.
        if self.pending is None or not self.pending.is_fresh(self.clock()) or announcement.priority >= self.pending.priority:
            if self.pending:self._notify(self.pending,"dropped")
            self.pending = replace(announcement,accepted_ns=time.perf_counter_ns())
            self._notify(self.pending,"accepted")
            return True
        self._notify(announcement,"dropped")
        return False

    def take(self, available, *, defer_start=False):
        if self.pending is not None and not self.pending.is_fresh(self.clock()):
            self._notify(self.pending,"dropped")
            self.pending = None
        ready_at = self.next_ready_at()
        if not available or ready_at is None or self.clock() < ready_at or self.inflight is not None:
            return None
        result, self.pending = self.pending, None
        if defer_start:self.inflight=result
        else:self.started(result)  # Compatibility for existing synchronous consumers.
        return result

    def next_ready_at(self):
        if self.pending is None:
            return None
        # One verified lock transition should follow the hover naturally, rather
        # than sitting behind the cooldown intended for repeated idle comments.
        gap = 0 if self.pending.event_key in ("pick-locked", "draft-picks") or (
            self.pending.event_key == "pick-hover" and self._last_event_key == "champ-select") else self.cooldown
        return max(self.pending.not_before, self.last_started + gap)

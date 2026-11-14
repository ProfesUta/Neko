"""Assigned position is API evidence, not a guess about a champion or location."""
from dataclasses import dataclass, asdict
from threading import Lock

ROLES = frozenset(("TOP","JUNGLE","MIDDLE","BOTTOM","UTILITY"))
LIVE_PHASES = frozenset(("GameStart","InProgress","Reconnect"))


@dataclass(frozen=True)
class LaneContext:
    role: str | None = None
    role_source: str = "unavailable"
    assigned_lane: str | None = None
    mode: str | None = None
    map_number: int | None = None
    team: str = ""
    player_champion: str = ""
    epoch: int = 0
    supported: bool = False

    def to_dict(self):
        return asdict(self)


class LaneTracker:
    def __init__(self, fallback="UNKNOWN", on_change=None):
        self._lock = Lock()
        self.fallback = fallback if fallback in ROLES else None
        self._role = None
        self._source = "unavailable"
        self._phase = "Unavailable"
        self._pending_selection = False
        self._context = LaneContext()
        self.on_change = on_change

    def snapshot(self):
        with self._lock:
            return self._context

    def _publish(self, mode=None, map_number=None, team="", champion="", epoch=0):
        role = self._role or self.fallback
        source = self._source if self._role else "user-configured fallback" if role else "unavailable"
        supported = mode == "CLASSIC" and map_number == 11
        lane = {"TOP":"TOP","MIDDLE":"MIDDLE","BOTTOM":"BOTTOM","UTILITY":"BOTTOM"}.get(role) if supported else None
        state = LaneContext(role,source,lane,mode,map_number,team,champion,epoch,supported)
        changed = state != self._context
        self._context = state
        return state if changed else None

    def _notify(self, changed):
        if changed and self.on_change:
            self.on_change(changed)

    def phase(self, phase, role="", selection_known=False):
        with self._lock:
            previous = self._phase
            self._phase = phase
            if phase == "ChampSelect":
                if previous not in ("ChampSelect","Unavailable"):
                    self._role, self._source = None,"unavailable"
                self._pending_selection = True
                if selection_known and role in ROLES:
                    self._role, self._source = role,"League client assigned position"
                changed = self._publish(epoch=self._context.epoch)
            elif phase in LIVE_PHASES:
                changed = self._publish(epoch=self._context.epoch) if phase == "GameStart" and previous not in LIVE_PHASES else None
            elif phase != "Unavailable" and phase != "None":
                self._role, self._source, self._pending_selection = None,"unavailable",False
                changed = self._publish(epoch=self._context.epoch)
            else:
                changed = None  # Short loading/transport gaps retain assignment.
        self._notify(changed)

    def update(self, epoch, mode, map_number, role="", team="", champion=""):
        with self._lock:
            if epoch != self._context.epoch and not self._pending_selection:
                self._role,self._source = None,"unavailable"
            if role in ROLES:
                self._role,self._source = role,"Live Client assigned position"
            self._pending_selection = False
            changed = self._publish(mode if isinstance(mode,str) else None,
                                    map_number if type(map_number) is int else None,
                                    team if team in ("ORDER","CHAOS") else "",champion[:80],epoch)
        self._notify(changed)

    def reset(self, epoch):
        with self._lock:
            self._role,self._source,self._pending_selection = None,"unavailable",False
            changed = self._publish(epoch=epoch)
        self._notify(changed)

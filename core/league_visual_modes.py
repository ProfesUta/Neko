"""Local observation and explicitly scoped replay tests are separate permissions."""
import hashlib
import json
from enum import Enum
from pathlib import Path

import config
from core.league_visual import VisualCalibration


class VisionMode(str, Enum):
    OFF = "OFF"
    LIVE_OBSERVATION = "LIVE_OBSERVATION"
    CONTROLLED_TEST = "CONTROLLED_TEST"
    APPROVED_ALERTS = "APPROVED_ALERTS"


def configured_mode():
    value=getattr(config,"LEAGUE_VISION_MODE","")
    if not value:return VisionMode.LIVE_OBSERVATION if config.LEAGUE_VISION_ENABLED else VisionMode.OFF
    try:
        mode=VisionMode(value)
        return VisionMode.LIVE_OBSERVATION if mode==VisionMode.CONTROLLED_TEST else mode
    except ValueError:return VisionMode.OFF


def speech_permission(mode, *, replay_valid=False, session_active=False, now=0, deadline=0):
    if mode==VisionMode.CONTROLLED_TEST:
        if replay_valid and not session_active and now<deadline:return True,"Explicit local recorded-replay test"
        return False,"Recorded test ended or a League session became active"
    if mode==VisionMode.APPROVED_ALERTS:
        # There is no verified Riot deployment authorization in this build.
        # An environment variable, local JSON receipt or checkbox cannot grant it.
        return False,"Verified Riot approval has not been provisioned; configuration flags are not approval"
    return False,"Observation only: automatic enemy-warning speech is disabled"


def roster_key(roster):
    return tuple(sorted((p.get("champion",""),p.get("side","")) for p in roster))


def prepare_live_profile(path, league, state, cancellation):
    """One cached derived profile per actual roster; never rewrites recording calibration.

    Runs on the existing visual worker. Official static references are loaded or
    fetched once at match setup, never from the capture/UI/conversation threads.
    """
    from core.cancellation import check_cancelled
    from core.league_icons import prepare_icons
    from core.league_visual_evaluation import file_digest,save_json
    base=VisualCalibration.load(path)
    roster=league.roster()
    if (not isinstance(roster,(list,tuple)) or not 1<=len(roster)<=10 or any(not isinstance(p,dict) for p in roster)
            or any(p.get("side") not in ("ally","enemy") for p in roster)
            or any(not isinstance(p.get("champion"),str) or not 1<=len(p["champion"])<=80 for p in roster)):
        raise ValueError("Waiting for current public champion roster data")
    roster=[dict(p) for p in roster]
    blocked=("Mirror picks need team-marker calibration" if len({p["champion"] for p in roster})!=len(roster)
             else "Waiting for known allied and enemy champions" if len(roster)<2 or {p["side"] for p in roster}!={"ally","enemy"} else "")
    key=hashlib.sha256(json.dumps([file_digest(path),roster_key(roster)],sort_keys=True).encode()).hexdigest()[:24]
    derived=config.DATA_DIR/"league-vision"/"live-profiles"/f"{key}.json"
    if not derived.is_file():
        spec={**base.spec,"roster":roster,"templates":{},"_template_sources":{},"viewport_nameplates":{},
              "blockers":[],"assigned_role":state.role or "UNKNOWN","player_champion":state.player_champion,
              "game_mode":state.mode,"map_number":state.map_number,"confirmation_policy":"strict3",
              "_source":"Current League API roster; official static icon references"}
        spec["recognition_blocked_reason"]=blocked
        # Recording-specific crops/regions are not proof of live HUD or location.
        if spec.get("calibration_scope")!="live_game_window":spec["regions"]=[]
        save_json(derived,spec)
    check_cancelled(cancellation)
    if not blocked:prepare_icons(derived,cancellation,knowledge=getattr(league,"knowledge",None))
    check_cancelled(cancellation)
    profile=VisualCalibration.load(derived)
    if roster_key(profile.spec["roster"])!=roster_key(roster):raise ValueError("Current roster changed during reference preparation")
    return VisualCalibration({**profile.spec,"roster":roster,"assigned_role":state.role or "UNKNOWN",
        "player_champion":state.player_champion,"game_mode":state.mode,"map_number":state.map_number},profile.directory)

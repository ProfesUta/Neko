"""Narrow deterministic questions; advice and unrelated conversation use Capri's model."""

import re

from core.league import direct_league_answer


def league_answer(text, client, verified_only=False):
    def unknown(message):
        return None if verified_only else message
    question = " ".join(text.casefold().replace("\u2019", "'").strip(" .?!").split())
    question = re.sub(r"^(?:hey )?capri[, ]+", "", question)
    select_kind = None
    if re.fullmatch(r"(?:what|which) champion am i hovering(?: over)?|what am i hovering", question):
        select_kind = "hover"
    elif re.fullmatch(r"who did i lock(?: in)?|what(?: champion)? did i lock(?: in)?|who have i locked in", question):
        select_kind = "lock"
    elif re.fullmatch(r"what role did i get|what(?:'s| is) my (?:assigned )?(?:role|position)|what lane did i get", question):
        select_kind = "role"
    elif re.fullmatch(r"(?:which|what) champions (?:have been|were|are) (?:picked|banned)|who (?:is|was|got) banned|what (?:are|were) the bans", question):
        select_kind = "bans" if "ban" in question else "picks"
    if select_kind:
        if not client.champ_select_enabled:
            return unknown("Champion-select awareness is disabled in my settings.")
        snapshot = client.champ_select.snapshot()
        if not snapshot.connected:
            return unknown("I can't read the League client right now, so I can't confirm your selection.")
        if snapshot.phase != "ChampSelect":
            return "You're not in champion select right now, chum."
        if select_kind == "hover":
            if snapshot.my_champion:
                return f"You've locked in {snapshot.my_champion}." if snapshot.my_pick_locked else f"You're hovering {snapshot.my_champion}, chum."
            return unknown("I don't see a champion hover yet.")
        if select_kind == "lock":
            return f"You locked in {snapshot.my_champion}, chum." if snapshot.my_pick_locked and snapshot.my_champion else unknown("You haven't locked a champion in yet.")
        if select_kind == "role":
            role = {"UTILITY": "support", "BOTTOM": "bottom", "MIDDLE": "mid", "TOP": "top", "JUNGLE": "jungle"}.get(snapshot.role)
            return f"You got {role}." if role else unknown("I can't read your assigned position yet.")
        if select_kind == "bans":
            return "Bans so far: " + ", ".join(snapshot.bans) + "." if snapshot.bans else unknown("No confirmed bans yet.")
        pieces = []
        for label, champions in (("Allies locked", snapshot.allied_champions), ("Enemies locked", snapshot.enemy_champions),
                                 ("Allied hovers", snapshot.allied_hovers), ("Visible enemy hovers", snapshot.enemy_hovers)):
            if champions:
                pieces.append(label + ": " + ", ".join(champions))
        return "; ".join(pieces) + "." if pieces else unknown("No visible picks yet.")
    event_kind = None
    if re.fullmatch(r"what (?:just )?happened in my (?:game|match)|what just happened|what(?:'s| was| is) the (?:most )?recent event", question):
        event_kind = "recent"
    elif re.fullmatch(r"what was the last objective|what(?:'s| is) the last objective|what objective (?:just fell|was taken)", question):
        event_kind = "objective"
    elif re.fullmatch(r"did (?:we|my team|our team) (?:take|get|kill|secure) (?:the )?(dragon|baron|herald)(?: yet)?", question):
        event_kind = question.split()[-1] if not question.endswith(" yet") else question.split()[-2]
    if event_kind:
        # 'What just happened?' outside an active game remains normal conversation.
        if event_kind == "recent" and question == "what just happened" and not client.events():
            return None
        if not client.events_enabled:
            return unknown("Live match events are disabled in my settings.")
        events = client.events()
        if not events:
            return unknown("I don't have a current match event feed, so I can't confirm what happened.")
        wanted = {"dragon": ("DragonKill",), "baron": ("BaronKill",), "herald": ("HeraldKill",),
                  "objective": ("DragonKill", "BaronKill", "HeraldKill", "HordeKill", "TurretKilled", "InhibKilled")}.get(event_kind)
        event = next((event for event in reversed(events) if wanted is None or event.name in wanted), None)
        if event is None:
            return unknown("No matching event is in my recent match history.")
        prefix = f"At {int(event.game_time)//60}:{int(event.game_time)%60:02d}, "
        if event_kind in ("dragon", "baron", "herald") and not event.side:
            return prefix + event.describe().rstrip(".") + "; I can't confirm which team took it."
        return prefix + event.describe()
    if client.stats_enabled:
        return direct_league_answer(question, client.read())
    return None

"""Run: python -B benchmark_league.py [--live] [--seconds 5] [--output report.json]

Default: a labelled synthetic API exposure/polling experiment at 250/500ms,
plus local LCU parsing, HTTP/JSON/dedup and direct-answer microbenchmarks.
--live additionally reads Riot's fixed loopback APIs without audio or game input.
No tokens, player identities, screenshots or match payloads are printed/saved.
TTS start timing is collected during normal Capri playback when
LEAGUE_LATENCY_METRICS_ENABLED is enabled (context.league.metrics.report()).
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

import config
from core.league import LeagueClient
from core.league_champ_select import LockfileDiscovery, PHASE_PATH, local_tls
from core.league_metrics import LeagueMetrics


async def microbench():
    cpu_started = time.process_time()
    wall_started = time.perf_counter_ns()
    metrics = LeagueMetrics(True)
    client = LeagueClient(metrics)
    client.champ_select.names.names = {103: "Ahri"}
    packet = json.dumps([8, "OnJsonApiEvent", {"uri": "/lol-champ-select/v1/session", "data": {
        "localPlayerCellId": 1, "myTeam": [{"cellId": 1, "assignedPosition": "middle", "championPickIntent": 103}],
        "actions": [], "bans": {}}}])
    for _ in range(2000):
        client.champ_select.handle_message(packet)
        client.answer("What champion am I hovering?")
    data = {"Events": [{"EventID": index, "EventName": "ChampionKill", "EventTime": float(index),
                         "KillerName": "fixture-a", "VictimName": "fixture-b"} for index in range(300)]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as http:
        for _ in range(500):
            await client.poll_events(http)
    previous = config.LEAGUE_PROACTIVE_VOICE_ENABLED
    config.LEAGUE_PROACTIVE_VOICE_ENABLED = True
    client.on_notification = lambda notification: None
    try:
        for _ in range(1000):
            client._notify("fixture", "Synthetic acknowledgment")
    finally:
        config.LEAGUE_PROACTIVE_VOICE_ENABLED = previous
    return {"kind": "synthetic in-process + httpx MockTransport; no real League or audio",
            "existing_events_per_response": 300, "wall_ms": (time.perf_counter_ns() - wall_started) / 1e6,
            "process_cpu_seconds": time.process_time() - cpu_started, "metrics": metrics.report()}


async def polling(interval, seconds, live=False):
    metrics = LeagueMetrics(True)
    league = LeagueClient(metrics)
    started = time.monotonic()
    cpu = time.process_time()
    polls, detected = 0, 0
    exposed = []
    next_exposure = .137
    def handle(request):
        nonlocal next_exposure
        now = time.monotonic() - started
        while next_exposure <= now:
            exposed.append({"EventID": len(exposed), "EventName": "ChampionKill", "EventTime": next_exposure})
            next_exposure += .137
        return httpx.Response(200, json={"Events": exposed})
    options = {"verify": local_tls()} if live else {"transport": httpx.MockTransport(handle)}
    async with httpx.AsyncClient(timeout=.4, trust_env=False, follow_redirects=False, **options) as http:
        # A zero-event synthetic baseline precedes all exposures.
        if not await league.poll_events(http):
            return {"interval_ms": interval * 1000, "available": False, "reason": "Live Client API unavailable"}
        while time.monotonic() - started < seconds:
            before = time.monotonic()
            old_id = league.tracker.high_id
            ok = await league.poll_events(http)
            polls += 1
            if ok:
                for event in league.events():
                    if event.event_id is not None and event.event_id > old_id:
                        detected += 1
                        if not live:
                            metrics.record("synthetic_exposed_to_detected", int((event.received_at - started - event.game_time) * 1e9))
            await asyncio.sleep(max(0, interval - (time.monotonic() - before)))
    return {"interval_ms": interval * 1000, "available": True, "polls": polls,
            "new_events": detected, "wall_seconds": time.monotonic() - started,
            "process_cpu_seconds": time.process_time() - cpu,
            "kind": "actual Riot local API; exposure time unknown" if live else "synthetic API; known exposure clock",
            "metrics": metrics.report()}


async def lcu_probe():
    credentials = LockfileDiscovery().discover()
    if credentials is None:
        return {"available": False, "reason": "No lockfile"}
    # Credentials only go to this fixed URL. Never put them in the result.
    try:
        async with httpx.AsyncClient(base_url=f"https://127.0.0.1:{credentials.port}",
                                     headers={"Authorization": credentials.authorization},
                                     verify=local_tls(), trust_env=False, follow_redirects=False, timeout=.4) as http:
            response = await http.get(PHASE_PATH)
            response.raise_for_status()
            phase = response.json()
            return {"available": True, "phase": phase if isinstance(phase, str) else "malformed"}
    except (httpx.HTTPError, ValueError):
        return {"available": False, "reason": "LCU request unavailable"}


async def run(args):
    result = {"microbench": await microbench(),
              "polling": [await polling(interval, args.seconds) for interval in (.25, .5)],
              "limits": ["Synthetic exposure timing is not Riot server/event latency.",
                         "No microphone, TTS synthesis or speaker playback in this benchmark.",
                         "Real event exposure time cannot be recovered exactly from EventTime."]}
    if args.live:
        result["lcu"] = await lcu_probe()
        result["live_polling"] = [await polling(interval, args.seconds, True) for interval in (.25, .5)]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Also measure the read-only Riot APIs; never plays audio")
    parser.add_argument("--seconds", type=float, default=5, help="Seconds per polling cadence")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not 1 <= arguments.seconds <= 60:
        parser.error("--seconds must be between 1 and 60")
    config.LEAGUE_CHAMP_SELECT_ENABLED = True  # Synthetic answers are measured even with CAPRI_LEAGUE=0.
    report = asyncio.run(run(arguments))
    output = json.dumps(report, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(output + "\n", encoding="utf-8")
    print(output)

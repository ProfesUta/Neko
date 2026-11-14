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
import tempfile
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
    if getattr(args,"real_vision_report",None):
        from core.league_visual_evaluation import analyze_minimap_cache
        report=analyze_minimap_cache(args.real_vision_report,args.real_vision_calibration,args.real_vision_output)
        return {"real_vision":{key:report[key] for key in ("input","analyzed_frames","metrics","cpu_seconds","wall_seconds","cpu_percent_one_core")},
                "report":str(Path(args.real_vision_output)/"detections.json")}
    result = {"microbench": await microbench(),
              "polling": [await polling(interval, args.seconds) for interval in (.25, .5)],
              "limits": ["Synthetic exposure timing is not Riot server/event latency.",
                         "No microphone, TTS synthesis or speaker playback in this benchmark.",
                         "Real event exposure time cannot be recovered exactly from EventTime."]}
    if args.live:
        result["lcu"] = await lcu_probe()
        result["live_polling"] = [await polling(interval, args.seconds, True) for interval in (.25, .5)]
    if getattr(args,"companion",False):
        result["companion"] = companion_microbench()
    if getattr(args,"replay",False):
        result["replay"] = replay_microbench()
    if getattr(args,"vision",False):
        result["vision"] = vision_microbench()
    return result


def companion_microbench():
    from core.league_knowledge import LeagueKnowledge
    metrics=LeagueMetrics(True)
    cpu=time.process_time()
    client=LeagueClient()
    if not client.champ_select.names.names:
        client.champ_select.names.names={64:"Lee Sin"}
    previous=(config.LEAGUE_LIVE_DATA_ENABLED,config.LEAGUE_KNOWLEDGE_ENABLED,config.LEAGUE_STATIC_DOWNLOADS_ENABLED)
    config.LEAGUE_LIVE_DATA_ENABLED=config.LEAGUE_KNOWLEDGE_ENABLED=True
    config.LEAGUE_STATIC_DOWNLOADS_ENABLED=False
    try:
        with tempfile.TemporaryDirectory(prefix="capri-league-benchmark-") as directory:
            knowledge=LeagueKnowledge(client.champ_select.names,Path(directory))
            knowledge.version="0.0.1"
            knowledge._save("0.0.1-champion.json",{"data":{"LeeSin":{"name":"Lee Sin"}}})
            knowledge._save("0.0.1-champion-LeeSin.json",{"data":{"LeeSin":{"name":"Lee Sin","tags":["Fighter"],
                "passive":{"name":"Passive","description":"Synthetic fixture."},
                "spells":[{"name":slot,"description":"Synthetic static reference."} for slot in "QWER"]}}})
            client.knowledge=knowledge
            client._snapshot={"champion":"Lee Sin","game":{"gameTime":300},"scores":{"kills":2,"deaths":1,"assists":4},"items":["Boots"]}
            client._captured_at=time.monotonic()
            for name,question in (("cached_ability","What does Lee Sin Q do?"),("cached_inventory","What items have I bought?"),
                                  ("cached_kda","What's my current KDA?"),("unrelated_gate","What's on your mind?")):
                client.companion_answer(question)
                for _ in range(2000):
                    start=time.perf_counter_ns()
                    client.companion_answer(question)
                    metrics.record(name,time.perf_counter_ns()-start)
            knowledge.close()
    finally:
        config.LEAGUE_LIVE_DATA_ENABLED,config.LEAGUE_KNOWLEDGE_ENABLED,config.LEAGUE_STATIC_DOWNLOADS_ENABLED=previous
    return {"kind":"actual Python processing with synthetic cached reference/stats; no live game, network, model or audio",
            "champion_name_count":len(client.champ_select.names.names),"metrics":metrics.report(),
            "process_cpu_seconds":time.process_time()-cpu}


def replay_microbench():
    import cv2
    import numpy as np
    from core.league_replay import MinimapDetector
    metrics=LeagueMetrics(True)
    cpu=time.process_time()
    before=cv2.getNumThreads()
    cv2.setNumThreads(1)
    try:
        with tempfile.TemporaryDirectory(prefix="capri-replay-benchmark-") as directory:
            root=Path(directory)
            rng=np.random.default_rng(482)
            templates={f"Fixture{index}":rng.integers(0,255,(24,24,3),dtype="uint8") for index in range(10)}
            for name,image in templates.items():cv2.imwrite(str(root/(name+".png")),image)
            spec={"reference_size":[1920,1080],"minimap":[.8,.7,.18,.29],"perspective":"player","fog_of_war":"player",
                  "roster":[{"champion":name,"side":"enemy" if i<5 else "ally","position":"JUNGLE" if i==0 else ""}
                            for i,name in enumerate(templates)],"templates":{name:name+".png" for name in templates}}
            detector=MinimapDetector(spec,root)
            blank=np.zeros((1080,1920,3),dtype="uint8")
            visible=blank.copy();visible[796:820,1576:1600]=templates["Fixture0"]
            accepted=rejected=0
            for _ in range(250):
                start=time.perf_counter_ns();found,_=detector.detect(visible)
                metrics.record("visible_icon_frame",time.perf_counter_ns()-start)
                accepted+=found is not None
                start=time.perf_counter_ns();found,_=detector.detect(blank)
                metrics.record("absent_icon_frame",time.perf_counter_ns()-start)
                rejected+=found is None
    finally:
        cv2.setNumThreads(before)
    return {"kind":"actual single-thread OpenCV on synthetic 1080p frames / 10 calibrated templates; no video decoding or real minimap icons",
            "metrics":metrics.report(),"synthetic_positive_detections":accepted,"synthetic_absence_rejections":rejected,
            "process_cpu_seconds":time.process_time()-cpu}


def vision_microbench():
    import cv2
    import numpy as np
    from core.league_visual import VisualCalibration,LeagueVisualPipeline,VisualAlertDispatcher
    metrics=LeagueMetrics(True)
    before=cv2.getNumThreads();cv2.setNumThreads(1)
    cpu=time.process_time();wall=time.perf_counter()
    detected=0;viewport_detected=0
    try:
        with tempfile.TemporaryDirectory(prefix="capri-vision-benchmark-") as directory:
            root=Path(directory);rng=np.random.default_rng(482)
            icons={f"Fixture{i}":rng.integers(0,255,(24,24,3),dtype="uint8") for i in range(10)}
            for name,image in icons.items():cv2.imwrite(str(root/(name+".png")),image)
            spec={"reference_size":[1920,1080],"minimap":[.8,.7,.18,.29],"perspective":"player","fog_of_war":"player",
                  "roster":[{"champion":name,"side":"enemy" if i<5 else "ally"} for i,name in enumerate(icons)],
                  "templates":{name:name+".png" for name in icons},"game_mode":"CLASSIC","map_number":11,
                  "assigned_role":"MIDDLE","player_champion":"Fixture5","fps":10,"alerts_muted":False,
                  "regions":[{"label":"synthetic mid","bounds":[0,0,1,1],"lane":"MIDDLE"}]}
            profile=VisualCalibration(spec,root);pipeline=LeagueVisualPipeline(profile);lane=profile.offline_lane()
            dispatcher=VisualAlertDispatcher(profile)
            visible=np.zeros((1080,1920,3),np.uint8)
            for i in range(5):visible[786+30*i:810+30*i,1556+50*i:1580+50*i]=icons[f"Fixture{i}"]
            visible[996:1020,1786:1810]=icons["Fixture5"]
            regions=profile.regions_from_frame(visible)
            for i in range(10):pipeline.process(regions,lane,i*.1)
            for i in range(250):
                at=1+i*.1;started=time.perf_counter_ns()
                found=pipeline.process(regions,lane,at)
                metrics.finish("five_enemies_plus_player_minimap",started);detected+=len(found)
                started=time.perf_counter_ns();dispatcher.update(found,1,now=at)
                metrics.finish("notification_proposals",started)
            from core.league_visual import ViewportMatcher
            viewport_spec={**spec,"viewport":[0,0,.5,.5],"blockers":[{"region":[.9,0,.05,.05],"image":"unused.png"}],
                           "viewport_nameplates":{}}
            view=np.zeros((540,960,3),np.uint8)
            for i,name in enumerate(icons):
                plate=rng.integers(50,240,(12,40),dtype="uint8")
                cv2.imwrite(str(root/(name+"-plate.png")),plate)
                viewport_spec["viewport_nameplates"][name]=[name+"-plate.png"]
                if i<5:
                    x,y=40+i*170,40+i*45
                    view[y:y+12,x:x+40]=plate[:,:,None]
                    view[y+15:y+18,x-10:x+60]=(0,0,255)
            matcher=ViewportMatcher(VisualCalibration(viewport_spec,root))
            for i in range(5):matcher.detect(view,[f"Fixture{i}" for i in range(5)],0,lane)
            for i in range(100):
                started=time.perf_counter_ns()
                found=matcher.detect(view,[f"Fixture{i}" for i in range(5)],i*.1,lane)
                metrics.finish("five_enemy_nameplates_with_red_bars",started);viewport_detected+=len(found)
    finally:cv2.setNumThreads(before)
    elapsed=time.perf_counter()-wall;cpu=time.process_time()-cpu
    return {"kind":"actual single-thread CPU OpenCV; synthetic cached 1080p image / 10 references; no game, capture, GPU, LLM or speech",
            "metrics":metrics.report(),"confirmed_observations":detected,"expected_observations":1250,
            "viewport_observations":viewport_detected,"expected_viewport_observations":500,
            "process_cpu_seconds":cpu,"wall_seconds":elapsed,"cpu_percent_one_core":100*cpu/elapsed,
            "threads":1,"warmup_frames":10}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Also measure the read-only Riot APIs; never plays audio")
    parser.add_argument("--seconds", type=float, default=5, help="Seconds per polling cadence")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--companion",action="store_true",help="Also benchmark cached reference/live-query routes without network")
    parser.add_argument("--replay",action="store_true",help="Explicit synthetic OpenCV replay detector benchmark (optional dependency)")
    parser.add_argument("--vision",action="store_true",help="Phase 6: all five enemies plus player, temporal confirmation and notification proposals (synthetic)")
    parser.add_argument("--real-vision-report",type=Path,help="Use the explicit lossless minimap cache from a real detect run")
    parser.add_argument("--real-vision-calibration",type=Path)
    parser.add_argument("--real-vision-output",type=Path)
    arguments = parser.parse_args()
    if arguments.real_vision_report and (not arguments.real_vision_calibration or not arguments.real_vision_output):
        parser.error("Cached real-footage evaluation needs --real-vision-calibration and --real-vision-output")
    if not 1 <= arguments.seconds <= 60:
        parser.error("--seconds must be between 1 and 60")
    config.LEAGUE_CHAMP_SELECT_ENABLED = True  # Synthetic answers are measured even with CAPRI_LEAGUE=0.
    report = asyncio.run(run(arguments))
    output = json.dumps(report, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(output + "\n", encoding="utf-8")
    print(output)

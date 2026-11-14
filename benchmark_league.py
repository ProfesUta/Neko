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
        if getattr(args,"reference_variants",None):
            return reference_variant_benchmark(args.real_vision_report,args.real_vision_calibration,args.real_vision_output,
                                               args.reference_variants,args.vision_labels)
        if getattr(args,"paired_vision_baseline",None):
            result=paired_vision_processing(args.real_vision_report,args.real_vision_calibration,args.paired_vision_baseline)
            from core.league_visual_evaluation import save_json
            save_json(Path(args.real_vision_output)/"paired-processing.json",result)
            return result
        if getattr(args,"simulate_notifications",False):
            from core.league_visual_evaluation import simulate_visual_notifications,save_json
            factory=None
            if args.notification_baseline_revision:
                factory=lambda profile,clock:baseline_visual_policy(args.notification_baseline_revision,profile,clock)
            result=simulate_visual_notifications(args.real_vision_report,args.vision_labels,baseline_factory=factory,
                busy_intervals=((3.5,5.),(180.6,181.2)) if args.simulated_user_busy else ())
            # Known construction truth for continuous-sighting deduplication;
            # deliberately separate from real-footage accuracy and real audio.
            from core.league_replay import read_json
            from core.league_visual import VisualObservation
            from dataclasses import asdict
            original=read_json(args.real_vision_report,20_000_000)
            spec=read_json(original["calibration"],65536)
            champion=next(p["champion"] for p in spec["roster"] if p["side"]=="enemy")
            folder=Path(args.real_vision_output)/"continuous-fixture";folder.mkdir(parents=True,exist_ok=True)
            with (folder/"frames.jsonl").open("w",encoding="utf-8") as stream:
                for i in range(200):
                    item=VisualObservation(champion,"enemy","the minimap",.99,True,i*.1,"minimap",(.2,.2),(.16,.16,.08,.08),first_seen_at=0)
                    stream.write(json.dumps({"timestamp":i*.1,"observations":[asdict(item)]})+"\n")
            path=save_json(folder/"detections.json",{"kind":"synthetic continuous observation, no video pixels",
                "calibration":original["calibration"],"frames":"frames.jsonl","analyzed_fps":10})
            fixture=simulate_visual_notifications(path,baseline_factory=factory,
                busy_intervals=((.1,1.2),) if args.simulated_user_busy else ())
            result["synthetic_continuous_fixture"]={"expected_starts_once":1,"simulation":fixture}
            save_json(Path(args.real_vision_output)/"notification-simulation.json",result)
            return result
        if getattr(args,"confirmation_policies",False):
            from core.league_visual_evaluation import compare_confirmation_policies
            return compare_confirmation_policies(args.real_vision_report,args.real_vision_calibration,
                                                 args.real_vision_output,args.vision_labels)
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


def baseline_module(revision,name,clock=None):
    import re,subprocess,sys,types
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}",revision):raise ValueError("Use a pinned hexadecimal baseline commit")
    source=subprocess.check_output(["git","show",f"{revision}:core/{name}.py"],cwd=Path(__file__).parent)
    module=types.ModuleType("_capri_benchmark_baseline_"+name)
    sys.modules[module.__name__]=module
    exec(compile(source,f"{revision}:core/{name}.py","exec"),module.__dict__)
    if clock is not None:module.time=types.SimpleNamespace(monotonic=lambda:clock[0],perf_counter_ns=time.perf_counter_ns)
    return module


def baseline_visual_policy(revision,profile,clock):
    """Explicit offline comparison with trusted repository code at a pinned commit.

    Private module clocks prevent changing runtime/global clocks. No old worker,
    capture, API client, TTS or GUI is started. Git is only read when requested.
    """
    from core.league_voice import LeagueAnnouncement
    class SimAnnouncement(LeagueAnnouncement):
        def is_fresh(self,now=None):return super().is_fresh(clock[0] if now is None else now)
    modules=[]
    for name in ("league_visual","league_voice"):
        module=baseline_module(revision,name,clock)
        module.LeagueAnnouncement=SimAnnouncement
        modules.append(module)
    dispatcher=modules[0].VisualAlertDispatcher(profile);dispatcher.muted=False
    return dispatcher,modules[1].LeagueVoicePolicy()


def paired_vision_processing(source_report,calibration,revision):
    """Interleaved actual old/new pipelines, identical real pixels, no audio."""
    import cv2,numpy as np
    from core.league_visual import LeagueVisualPipeline,VisualCalibration
    from core.league_replay import local_file,read_json
    source_path=local_file(source_report);source=read_json(source_path,20_000_000)
    profile=VisualCalibration.load(calibration);cache=source.get("minimap_cache",{})
    if list(profile.reference)!=cache.get("reference_size") or list(profile.minimap)!=cache.get("minimap"):
        raise ValueError("Paired comparison requires identical cached minimap geometry")
    pixels=np.load(local_file(source_path.parent/cache["pixels"]),mmap_mode="r",allow_pickle=False)
    times=np.load(local_file(source_path.parent/cache["timestamps"]),mmap_mode="r",allow_pickle=False)
    if pixels.dtype!=np.uint8 or pixels.nbytes>1024**3 or pixels.shape[0]!=len(times):raise ValueError("Invalid minimap cache")
    old=baseline_module(revision,"league_visual")
    old.MinimapDetector=baseline_module(revision,"league_replay").MinimapDetector
    threads=cv2.getNumThreads();cv2.setNumThreads(1)
    metrics=LeagueMetrics(True);cpu=time.process_time();wall=time.perf_counter()
    different=0;work={"before":0,"after":0}
    try:
        before=old.LeagueVisualPipeline(profile);after=LeagueVisualPipeline(profile);lane=profile.offline_lane()
        for i in range(min(20,len(times))):
            before.process({"minimap":pixels[i]},lane,float(times[i]));after.process({"minimap":pixels[i]},lane,float(times[i]))
        before.temporal.reset(lane.epoch);after.temporal.reset(lane.epoch)
        for i,at in enumerate(times):
            found={}
            pairs=(("before",before),("after",after)) if i%2==0 else (("after",after),("before",before))
            for name,pipeline in pairs:
                started=time.perf_counter_ns();thread=time.thread_time_ns()
                found[name]=pipeline.process({"minimap":pixels[i]},lane,float(at))
                elapsed=time.perf_counter_ns()-started;active=time.thread_time_ns()-thread
                metrics.record(name+"_visual_processing",elapsed);metrics.record(name+"_thread_cpu",active);work[name]+=active
            identities=lambda items:sorted((o.champion,tuple(o.position)) for o in items)
            different+=identities(found["before"])!=identities(found["after"])
    finally:cv2.setNumThreads(threads)
    cpu=time.process_time()-cpu;wall=time.perf_counter()-wall
    return {"measurement":"Paired actual old/new CPU OpenCV, alternating order, same real pixels; excludes decode/capture/audio",
        "baseline_revision":revision,"analyzed_frames":len(times),"warmup_frames":20,"metrics":metrics.report(),
        "differing_current_identity_frames":different,"process_cpu_seconds":cpu,"wall_seconds":wall,
        "cpu_percent_one_core":100*cpu/wall,"active_thread_cpu_seconds":{k:v/1e9 for k,v in work.items()},
        "estimated_core_percent_at_recorded_10fps":{k:v/1e9/source["duration_s"]*100 for k,v in work.items()},
        "estimate_limit":"CPU work per source-video second, NOT measured live-match CPU/FPS or GPU use"}


def reference_variant_benchmark(source_report,calibration,output,manifest,labels_paths):
    """Target-only ablations against ALL roster distractors, never replace working assets."""
    import cv2,numpy as np
    from core.league_replay import MinimapDetector,read_json,local_file
    from core.league_visual import VisualCalibration,VisualObservation,TemporalConfirmation
    from core.league_visual_evaluation import save_json,evaluate_labels,CandidateEpisodes,file_digest,validate_labels
    source_path=local_file(source_report);source=read_json(source_path,20_000_000);cache=source["minimap_cache"]
    calibration=local_file(calibration);profile=VisualCalibration.load(calibration)
    variants=read_json(manifest,65536)
    enemies={p["champion"] for p in profile.spec["roster"] if p["side"]=="enemy"}
    if not set(variants)<=enemies or len(variants)>5 or any(not isinstance(v,dict) or not 1<=len(v)<=4 for v in variants.values()):
        raise ValueError("Provide at most four local reference alternatives per known enemy")
    truths={Path(p).stem:validate_labels(read_json(p,20_000_000)) for p in labels_paths}
    if list(profile.reference)!=cache["reference_size"] or list(profile.minimap)!=cache["minimap"]:
        raise ValueError("Reference ablation needs identical cached geometry")
    pixels=np.load(local_file(source_path.parent/cache["pixels"]),mmap_mode="r",allow_pickle=False)
    times=np.load(local_file(source_path.parent/cache["timestamps"]),mmap_mode="r",allow_pickle=False)
    if pixels.dtype!=np.uint8 or pixels.nbytes>1024**3 or len(pixels)!=len(times):raise ValueError("Invalid cache")
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    result={"measurement":"Actual target-only OpenCV ablations; ALL ten roster identities remain competing references. Not full-pipeline timing.","variants":{}}
    threads=cv2.getNumThreads();cv2.setNumThreads(1)
    try:
        for champion,alternatives in variants.items():
            for label,filename in alternatives.items():
                folder=output/f"variant-{len(result['variants'])+1}"
                if folder.exists():raise ValueError("Use a fresh reference comparison output")
                folder.mkdir()
                templates={n:str(local_file(calibration.parent/f)) for n,f in profile.spec["templates"].items()}
                templates[champion]=str(local_file(calibration.parent/filename))
                spec={**profile.spec,"templates":templates,"target_champion":champion,"confirmation_policy":"strict3"}
                config_path=save_json(folder/"calibration.json",spec)
                d=MinimapDetector(spec,folder,allow_any_enemy=True,scale_factor=profile.hud_scale)
                tracker=TemporalConfirmation(profile.fps);metrics=LeagueMetrics(True);episodes=CandidateEpisodes(source["analyzed_fps"])
                cpu=time.process_time();wall=time.perf_counter()
                with (folder/"frames.jsonl").open("w",encoding="utf-8") as stream:
                    for i,at in enumerate(times):
                        at=float(at);started=time.perf_counter_ns();found=d.match(pixels[i],champion);raw=[]
                        if found:
                            x,y,w,h=found["pixel_box"]
                            raw=[VisualObservation(champion,"enemy","the minimap",found["template_similarity"],True,at,"minimap",
                                  found["minimap_position"],(x/d.width,y/d.height,w/d.width,h/d.height),identity_margin=found["ambiguity_margin"])]
                        confirmed=tracker.update(raw,at,1);metrics.finish("single_target_all_roster_comparison",started)
                        items=[o.to_dict() for o in confirmed];index=round(at*source["source_fps"])
                        stream.write(json.dumps({"frame":index,"timestamp":at,"observations":items})+"\n");episodes.update(items,at,index)
                report={**source,"calibration":str(config_path),"calibration_sha256":file_digest(config_path),"candidates":episodes.candidates,
                        "frames":"frames.jsonl","metrics":metrics.report(),"cpu_seconds":time.process_time()-cpu,
                        "wall_seconds":time.perf_counter()-wall,"input":"Target-only real-pixel reference ablation"}
                report.pop("minimap_cache",None)
                path=save_json(folder/"detections.json",report);scores={}
                for name,truth in truths.items():
                    if truth.get("complete_champions") and champion not in truth["complete_champions"]:continue
                    scoped={**truth,"complete_champions":[champion],"reviews":[],"labels":[l for l in truth["labels"] if l["champion"]==champion]}
                    labels=save_json(folder/(name+".json"),scoped)
                    scores[name]=evaluate_labels(path,labels,allow_equivalent_calibration=True)
                result["variants"][champion+":"+label]={"scores":scores,"metrics":metrics.report(),"report":str(path)}
    finally:cv2.setNumThreads(threads)
    save_json(output/"comparison.json",result)
    return result


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
    parser.add_argument("--confirmation-policies",action="store_true",help="Compare strict3, two_validated and adaptive on identical real raw detections, without audio")
    parser.add_argument("--vision-labels",type=Path,action="append",default=[],help="Scoped labels for policy comparison; repeat for tuning and test audits")
    parser.add_argument("--simulate-notifications",action="store_true",help="Replay processed detections through actual and controlled-region voice-policy scheduling; no audio")
    parser.add_argument("--notification-baseline-revision",help="Optional pinned local Git commit for an offline before/after policy comparison")
    parser.add_argument("--simulated-user-busy",action="store_true",help="Controlled user-priority intervals during notification simulation")
    parser.add_argument("--paired-vision-baseline",help="Pinned baseline Git commit for interleaved old/new full CV timing")
    parser.add_argument("--reference-variants",type=Path,help="JSON: champion -> alternative name -> local icon path, relative to calibration")
    arguments = parser.parse_args()
    if arguments.real_vision_report and (not arguments.real_vision_calibration or not arguments.real_vision_output):
        parser.error("Cached real-footage evaluation needs --real-vision-calibration and --real-vision-output")
    if (arguments.confirmation_policies or arguments.simulate_notifications) and not arguments.real_vision_report:
        parser.error("Policy comparison/simulation needs --real-vision-report")
    if not 1 <= arguments.seconds <= 60:
        parser.error("--seconds must be between 1 and 60")
    config.LEAGUE_CHAMP_SELECT_ENABLED = True  # Synthetic answers are measured even with CAPRI_LEAGUE=0.
    report = asyncio.run(run(arguments))
    output = json.dumps(report, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(output + "\n", encoding="utf-8")
    print(output)

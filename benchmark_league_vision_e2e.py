"""Explicit offline recorded League vision-to-Capri-voice measurement.

No competitive gate is enabled. --audio authorizes fixed test/verified replay
speech. --loopback passively inspects the output mix in RAM; pause other audio.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path


def output_metadata(sd):
    from voice.playback import _output_options
    try:
        options=_output_options(sd)
        return {"output_device":dict(sd.query_devices(kind="output")),"resolved_output":{
            "device":options.get("device"),"wasapi_settings":bool(options.get("extra_settings")),
            "description":dict(sd.query_devices(options.get("device"),"output"))}}
    except (sd.PortAudioError,ValueError):
        # Detection-only runs remain usable on CI/hosts without an output device.
        # Audio attempts will report missing/error observations, never fake timing.
        return {"output_unavailable":"sounddevice could not discover the configured output"}


def calibration_workflow(video,calibration,manifest):
    from PySide6.QtWidgets import QApplication,QPushButton,QCheckBox
    from core.league_visual_evaluation import save_json,file_digest
    from core.league_replay import read_json,local_file
    from core.league_visual import VisualCalibration
    from benchmarks.league_vision_e2e import fingerprint
    from gui.league_calibration import LeagueCalibrationDialog
    video=local_file(video);calibration=local_file(calibration);manifest=Path(manifest).resolve()
    copy=manifest.with_name(manifest.stem+"-calibration.json")
    if not copy.exists():
        spec=read_json(calibration,65536)
        spec["templates"]={n:str(local_file(calibration.parent/f)) for n,f in spec["templates"].items()}
        spec["viewport_nameplates"]={n:[str(local_file(calibration.parent/f)) for f in files] for n,files in spec.get("viewport_nameplates",{}).items()}
        for blocker in spec.get("blockers",[]):blocker["image"]=str(local_file(calibration.parent/blocker["image"]))
        save_json(copy,spec)
    app=QApplication.instance() or QApplication([])
    dialog=LeagueCalibrationDialog(copy,str(video))
    confirmed=QCheckBox("I inspected these visible sightings and their location evidence in the recording")
    dialog.layout().addWidget(confirmed)
    button=QPushButton("Confirm selected replay labels and export benchmark eligibility")
    def export():
        try:
            spec=dialog.read_spec();profile=VisualCalibration(spec,copy.parent);lane=profile.offline_lane()
            selected=[dialog.manifest["labels"][i.row()] for i in dialog.truth_table.selectionModel().selectedRows()]
            if not selected:raise ValueError("Visually inspect and select the sighting rows first")
            if not confirmed.isChecked():raise ValueError("Explicitly confirm that you reviewed the selected sightings and location evidence")
            if any(l["source"]=="minimap" and profile.region_evidence(l["position"],lane.assigned_lane)!="verified assigned lane region"
                   and not any(l["start_s"]<=p["video_s"]<=l["end_s"] for p in dialog.manifest.get("player_locations",[])) for l in selected):
                raise ValueError("Verify each selected sighting's actual assigned-lane region, or record independent exact-frame player points; do not infer Yuumi distance")
            if any(l["source"]=="viewport" for l in selected) and not profile.viewport_enabled:
                raise ValueError("Viewport sightings require calibrated nameplate/health-bar evidence")
            save_json(copy,spec)
            save_json(manifest,{"kind":"operator_confirmed_replay_eligibility","video":str(video),"video_fingerprint":fingerprint(video),
                "calibration":str(copy),"calibration_sha256":file_digest(copy),"selected_sightings":selected,
                "player_locations":dialog.manifest.get("player_locations",[]),
                "reviewer":{"kind":"operator_confirmed","method":"Manually inspected selected recorded frames and lane regions; no player-distance inference"}})
            dialog.message.setText("Saved explicit operator-confirmed replay whitelist: "+str(manifest))
        except (ValueError,OSError) as error:dialog.error(error)
    button.clicked.connect(export);dialog.layout().addWidget(button)
    dialog.message.setText("OFFLINE BENCHMARK: use Lane region and its actual lane, label visible intervals, select rows, then export. Original calibration is preserved.")
    dialog.show();dialog.load_frame();return app.exec()


def main(argv=None):
    from benchmarks.league_vision_e2e import SCENARIOS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation",choices=("run","calibrate","preflight"))
    parser.add_argument("--video",type=Path,default=Path("tests/fixtures/league_real_gameplay.mp4"))
    parser.add_argument("--calibration",type=Path,default=Path("data/league-vision/calibration.json"))
    parser.add_argument("--manifest",type=Path,default=Path("data/league-vision/e2e-eligibility.json"))
    parser.add_argument("--eligibility",choices=("verified","synthetic"),default="verified")
    parser.add_argument("--verified-index",type=int,default=0,help="Selected operator-whitelist sighting for --scenarios verified")
    parser.add_argument("--mode",choices=("replay","window"),default="replay")
    parser.add_argument("--scenarios",default="warwick,galio,overlap,two-enemies,synthetic,disappear,new-match,already-speaking,interrupt")
    parser.add_argument("--trials",type=int,default=20)
    parser.add_argument("--warmups",type=int,default=1)
    parser.add_argument("--audio",action="store_true")
    parser.add_argument("--loopback",action="store_true")
    parser.add_argument("--loopback-endpoint")
    parser.add_argument("--gpu-metrics",action="store_true",help="Optional finite nvidia-smi 1 Hz device-wide sampling")
    parser.add_argument("--contention",action="store_true",help="Explicit local Qwen/generated-image background load through existing model client")
    parser.add_argument("--labels",type=Path,nargs="*",default=[],help="Optional scoped exhaustive labels; score unique source frames once per policy")
    parser.add_argument("--output",type=Path,default=Path("artifacts/phase6_2/local-run"))
    args=parser.parse_args(argv)
    if not 1<=args.trials<=100 or not 0<=args.warmups<=3:parser.error("Use 1..100 trials and 0..3 warm-ups")
    scenarios=args.scenarios.split(",")
    if not set(scenarios)<=SCENARIOS.keys():parser.error("Unknown scenario; choose "+", ".join(SCENARIOS))
    if args.loopback and not args.audio:parser.error("Loopback requires explicit --audio")
    if args.operation=="calibrate":return calibration_workflow(args.video,args.calibration,args.manifest)
    return run(args,scenarios)


def run(args,scenarios):
    import platform
    from contextlib import ExitStack
    import cv2,numpy as np,sounddevice as sd
    import config
    from PySide6.QtWidgets import QApplication
    from core.league_visual import VisualCalibration
    from core.league_visual_evaluation import save_json
    from benchmarks.league_vision_e2e import Eligibility,load_clip,run_trial,write_results,SYNTHETIC,SCENARIOS,verified_clip,ensure_voice_ready
    metadata={"python":sys.version,"platform":platform.platform(),"logical_processors":os.cpu_count(),
        "opencv":cv2.__version__,"numpy":np.__version__,"mode":args.mode,"audio":args.audio,
        "live_gate":{"enabled":config.LEAGUE_VISION_ENABLED,"approved":config.LEAGUE_VISION_RIOT_APPROVED},
        **output_metadata(sd),
        "voice":{"speaker":config.KOKORO_SPEAKER_ID,"speed":config.KOKORO_SPEED,"threads":config.KOKORO_NUM_THREADS},"loopback_requested":args.loopback,
        "clock":"perf_counter_ns; source timestamps retained separately","source_audio":"Never opened or played"}
    args.output.mkdir(parents=True,exist_ok=True)
    if (args.output/"report.json").exists() and args.operation!="preflight":raise ValueError("Choose a fresh benchmark output folder")
    with ExitStack() as stack:
        probe=None
        if args.loopback:
            try:
                from benchmarks.wasapi_loopback import LoopbackProbe
                probe=stack.enter_context(LoopbackProbe(args.loopback_endpoint));time.sleep(.25);probe.arm()
                metadata["loopback_format"]=probe.format
            except (RuntimeError,OSError) as error:
                metadata["loopback_unavailable"]=str(error);probe=None
        if args.operation=="preflight":
            save_json(args.output/"preflight.json",metadata);print(json.dumps(metadata,indent=2));return 0
        if args.mode=="replay":os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
        app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
        profile=VisualCalibration.load(args.calibration)
        manifest=args.manifest if args.manifest.is_file() else None
        gate=Eligibility(args.video,args.calibration,manifest,synthetic=args.eligibility=="synthetic")
        if "verified" in scenarios:SCENARIOS["verified"]=verified_clip(gate,args.verified_index)
        metadata["eligibility"]="synthetic selected sighting" if gate.synthetic else "operator-confirmed manifest" if gate.spec else "none independently verified"
        old_metrics=config.LEAGUE_LATENCY_METRICS_ENABLED
        stack.callback(setattr,config,"LEAGUE_LATENCY_METRICS_ENABLED",old_metrics)
        config.LEAGUE_LATENCY_METRICS_ENABLED=True  # Private benchmark process only; approval flags untouched.
        old_threads=cv2.getNumThreads();cv2.setNumThreads(1);stack.callback(cv2.setNumThreads,old_threads)
        if args.audio:
            old_voice=config.LEAGUE_PROACTIVE_VOICE_ENABLED
            stack.callback(setattr,config,"LEAGUE_PROACTIVE_VOICE_ENABLED",old_voice)
            config.LEAGUE_PROACTIVE_VOICE_ENABLED=True
            from voice.tts import warm_up
            from voice.kokoro_session import session
            warm_up();stack.callback(session.shutdown)
        from benchmarks.league_e2e_workload import ModelWorkload,NvidiaSamples
        workload=ModelWorkload(args.contention);stack.callback(workload.close)
        gpu=NvidiaSamples(args.output/"gpu.csv",args.gpu_metrics);stack.callback(gpu.close)
        metadata["contention"]=workload.report()
        rows=[];trials=[];window=None;accuracy_frames={"strict3":{},"adaptive":{}}
        if args.mode=="window":
            from benchmarks.league_replay_window import ReplayWindow
            window=ReplayWindow(profile);stack.callback(window.dispose)
            metadata["T0_limit"]="Paint timestamp is not synchronized compositor presentation; appearance-to-output unavailable"
        for scenario in scenarios:
            frames,source=load_clip(args.video,profile,scenario,full=bool(window))
            metadata.setdefault("sources",{})[scenario]=source
            case_gate=Eligibility(args.video,args.calibration,None,synthetic=True) if scenario in SYNTHETIC else gate
            for pair in range(-args.warmups,args.trials):
                for policy in (("strict3","adaptive") if pair%2 else ("adaptive","strict3")):
                    observations=[] if args.labels and pair==0 and scenario not in SYNTHETIC else None
                    # Active native cancellation may discard the loaded worker.
                    # Verify, reuse, or account for reloading before warm trials.
                    readiness=ensure_voice_ready(warm_up,session) if args.audio else None
                    try:
                        current_rows,stats=run_trial(frames,profile,case_gate,scenario,policy,pair+args.warmups,
                            audio=args.audio,probe=probe,window=window,warmup=pair<0,observation_sink=observations)
                    except (ValueError,OSError,RuntimeError) as error:
                        metadata["incomplete_error"]=str(error)
                        workload.close();metadata["contention"]=workload.report();metadata["gpu_sampling"]=gpu.close()
                        write_results(args.output,rows,trials,metadata)
                        raise
                    stats.update(scenario=scenario,policy=policy,pair=pair,warmup=pair<0)
                    stats["voice_readiness"]=readiness
                    if observations is not None:
                        for item in observations:accuracy_frames[policy][round(item["timestamp"],6)]=item
                    stats["workload"]=workload.report()
                    rows.extend(current_rows);trials.append(stats)
                    write_results(args.output,rows,trials,metadata)
                    print(json.dumps({"scenario":scenario,"pair":pair,"policy":policy,"frames":stats["frames"],"notifications":stats["notifications"]}),flush=True)
                    until=time.perf_counter()+.3
                    while time.perf_counter()<until:app.processEvents();time.sleep(.005)
        metadata["gate_unchanged"]=(metadata["live_gate"]=={"enabled":config.LEAGUE_VISION_ENABLED,"approved":config.LEAGUE_VISION_RIOT_APPROVED})
        if args.labels:
            from core.league_visual_evaluation import evaluate_labels,file_digest
            from benchmarks.league_vision_e2e import fingerprint
            metadata["accuracy"]={}
            for policy,unique in accuracy_frames.items():
                if not unique:continue
                folder=args.output/("accuracy-"+policy);folder.mkdir(exist_ok=True)
                with (folder/"frames.jsonl").open("w",encoding="utf-8") as stream:
                    for _,item in sorted(unique.items()):stream.write(json.dumps(item,allow_nan=False)+"\n")
                save_json(folder/"source.json",{"video":str(args.video.resolve()),"calibration":str(args.calibration.resolve()),
                    "calibration_sha256":file_digest(args.calibration),"video_fingerprint":fingerprint(args.video),
                    "frames":"frames.jsonl","analyzed_fps":profile.fps,"candidates":[],"metrics":{},"cpu_seconds":None,"wall_seconds":None})
                for index,path in enumerate(args.labels):
                    result=evaluate_labels(folder/"source.json",path,allow_equivalent_calibration=True)
                    save_json(folder/f"audit-{index}.json",result)
                    metadata["accuracy"].setdefault(policy,[]).append({"labels":str(path),"result":result,
                        "scope":"Only source frames actually sampled in selected clips; repetitions deduplicated. Not full-match recall."})
        workload.close();metadata["contention"]=workload.report();metadata["gpu_sampling"]=gpu.close()
        write_results(args.output,rows,trials,metadata)
    return 0


if __name__=="__main__":
    try:raise SystemExit(main())
    except (ValueError,OSError,RuntimeError) as error:
        print("Offline benchmark error: "+str(error),file=sys.stderr);raise SystemExit(2)

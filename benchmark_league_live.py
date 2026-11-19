"""Finite, silent actual League-window observation using the existing workers.

Close Neko first to avoid duplicate League producers. No input automation, TTS,
LLM inference or default screenshots. --snapshot explicitly retains ONE cropped
minimap for subsequent visual validation; detections are not ground truth.
"""
import argparse
import time
from pathlib import Path
from threading import Event


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds",type=int,default=30)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--snapshot",action="store_true")
    args=parser.parse_args(argv)
    if not 5<=args.seconds<=120:parser.error("Use 5..120 seconds")
    if args.output.exists():parser.error("Choose a fresh output directory")
    import config
    from PySide6.QtCore import QCoreApplication
    from core.league import LeagueClient
    from core.league_visual_modes import VisionMode
    from core.league_visual_evaluation import save_json
    from gui.workers import LeagueWorker
    from gui.league_vision import LeagueVisualService
    from core.window_grounding import WindowGrounder
    import ctypes
    # Only this benchmark process. Approval/config flags cannot grant speech.
    config.LEAGUE_LIVE_DATA_ENABLED=True;config.LEAGUE_EVENTS_ENABLED=True;config.LEAGUE_CHAMP_SELECT_ENABLED=True
    config.LEAGUE_PROACTIVE_VOICE_ENABLED=False
    app=QCoreApplication.instance() or QCoreApplication([])
    league=LeagueClient();reader=LeagueWorker(league);service=LeagueVisualService(league)
    service.request_mode(VisionMode.LIVE_OBSERVATION)
    reader.session_active_changed.connect(service.sync);reader.visual_state_changed.connect(service.sync)
    args.output.mkdir(parents=True)
    api=ctypes.WinDLL("user32");api.GetForegroundWindow.restype=ctypes.c_void_p
    windows=[w for w in WindowGrounder().windows() if w.application.lower().replace(" ","")=="leagueoflegends.exe"]
    preflight={"foreground_hwnd":api.GetForegroundWindow(),"league_windows":[{"handle":w.handle,"pid":w.pid,
        "application":w.application,"bounds_physical":w.bounds.to_dict()} for w in windows]}
    reader.start();began=time.monotonic();wait=Event();samples=[];last_sample=0;debug=None;debug_observations=();last_context=None
    try:
        while time.monotonic()-began<args.seconds:
            app.processEvents();now=time.monotonic()
            if now-last_sample>=1:
                last_sample=now;service.refresh();status=service.status();samples.append(status)
                context=league.visual_context()
                if context:last_context=context.to_dict()
                print({"elapsed_s":round(now-began,1),"status":status["status"],"mode":status["mode"],
                       "frames":status.get("analyzed_frames",0),"fps":status.get("detection_fps",0)},flush=True)
            if args.snapshot and service.worker:
                service.worker.retain_frame=True
                with service.worker.lock:
                    if service.worker.last_frame:
                        debug=service.worker.last_frame;debug_observations=service.worker.last_frame_observations
            wait.wait(.02)
    finally:
        current=service.worker
        service.close();reader.stop()
        if current and not current.wait(5000):raise RuntimeError("Visual worker did not stop")
        if not reader.wait(5000):raise RuntimeError("League worker did not stop")
        app.processEvents()
    diagnostics=service.last_diagnostics
    state=league.lane.snapshot().to_dict()
    if debug:
        import cv2
        cv2.imwrite(str(args.output/"explicit-minimap.png"),debug.regions["minimap"])
        save_json(args.output/"explicit-minimap-observations.json",{"captured_at":debug.captured_at,
            "sequence":debug.sequence,"source_id":debug.source_id,"observations":list(debug_observations)})
    report={"mode":"LIVE_OBSERVATION","speech_jobs":0,"approval_granted":False,"preflight":preflight,
        "elapsed_s":time.monotonic()-began,"status_samples":samples,"last_run":diagnostics,"last_live_context":last_context,
        "post_shutdown_context":state,
        "snapshot_saved":bool(debug),"verified_detections":None,"false_or_ambiguous_detections":None,
        "recognition_note":"Confirmed CV observations are not independently verified sightings",
        "league_fps_impact":None,"fps_note":"No permitted API FPS measurement or paired manual game-FPS reading was performed",
        "measurement":"Actual game-window capture only if analyzed_frames > 0; no replay or mock substitution"}
    save_json(args.output/"report.json",report)
    print("Saved",args.output/"report.json")
    return 0


if __name__=="__main__":raise SystemExit(main())

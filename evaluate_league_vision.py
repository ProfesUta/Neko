"""Offline Phase 6 detection, manual labelling and evaluation. Never enables live capture."""
import argparse
import json
import os
import sys
from pathlib import Path


def calibration_command(video,calibration):
    arguments=[sys.executable,"-B",str(Path(__file__).resolve()),"label","--video",str(video),"--calibration",str(calibration)]
    if os.name=="nt":
        # PowerShell literal quoting also handles spaces, dollar signs and apostrophes.
        return "& "+" ".join("'"+value.replace("'","''")+"'" for value in arguments)
    import shlex
    return shlex.join(arguments)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="operation",required=True)
    detect=sub.add_parser("detect")
    detect.add_argument("video");detect.add_argument("--calibration",required=True);detect.add_argument("--output",required=True)
    detect.add_argument("--debug-overlays",action="store_true",help="Explicitly save up to 500 annotated frames locally")
    detect.add_argument("--cache-minimap",action="store_true",help="Explicitly save a lossless cropped-pixel cache for repeatable offline evaluation (max 1 GiB)")
    icons=sub.add_parser("icons",help="Automatically cache official champion icons; manual crops are optional")
    icons.add_argument("--calibration",default="data/league-vision/calibration.json")
    icons.add_argument("--replace",action="store_true",help="Explicitly replace existing references with official baselines")
    label=sub.add_parser("label")
    label.add_argument("--video",default=r"F:\Neko\tests\fixtures\league_real_gameplay.mp4")
    label.add_argument("--calibration",default="data/league-vision/calibration.json")
    label.add_argument("--detections",default="");label.add_argument("--labels",default="")
    evaluate=sub.add_parser("evaluate")
    evaluate.add_argument("detections");evaluate.add_argument("--labels",required=True);evaluate.add_argument("--output",required=True)
    evaluate.add_argument("--allow-equivalent-calibration",action="store_true",help="Reuse annotations only when reference resolution and region geometry are identical")
    sound=sub.add_parser("test-sound",help="Explicit controlled timing sound through existing playback; no live recording")
    sound.add_argument("--samples",type=int,default=1)
    args=parser.parse_args(argv)
    if args.operation=="test-sound" and not 1<=args.samples<=50:
        parser.error("Use 1..50 controlled tone samples")
    try:
        return run_operation(args)
    except (ValueError,OSError) as error:
        from core.league_replay import LocalFileError
        message=f"{parser.prog}: error: {error}\n"
        if args.operation=="detect" and isinstance(error,LocalFileError) and error.purpose=="Calibration file":
            message+=("\nCreate or repair this recording's calibration in the editor:\n"
                      +calibration_command(args.video,args.calibration)
                      +"\nDraw the minimap region, enter the known champion roster, and save calibration. "
                       "Missing official champion icons are cached automatically when detection starts.\n")
        parser.exit(2,message)


def run_operation(args):
    if args.operation=="icons":
        from core.league_icons import prepare_icons
        print(json.dumps(prepare_icons(args.calibration,replace=args.replace),indent=2));return 0
    if args.operation=="label":
        from PySide6.QtWidgets import QApplication
        from gui.league_calibration import LeagueCalibrationDialog
        app=QApplication.instance() or QApplication([])
        dialog=LeagueCalibrationDialog(args.calibration,args.video,detections=args.detections,labels=args.labels)
        dialog.show()
        if args.video:dialog.load_frame()
        return app.exec()
    if args.operation=="test-sound":
        from core.league_visual_sound import measure_test_sound
        print(json.dumps(measure_test_sound(args.samples),indent=2));return 0
    from core.league_visual_evaluation import analyze_recording,evaluate_labels,save_json
    if args.operation=="detect":
        report=analyze_recording(args.video,args.calibration,args.output,debug=args.debug_overlays,cache_minimap=args.cache_minimap)
        print(json.dumps({"analyzed_frames":report["analyzed_frames"],"candidate_count":len(report["candidates"]),"metrics":report["metrics"],"accuracy":None},indent=2))
    else:
        report=evaluate_labels(args.detections,args.labels,allow_equivalent_calibration=args.allow_equivalent_calibration)
        save_json(args.output,report);print(json.dumps(report,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())

"""Explicit offline blind review plans. Predictions never create reference labels."""
import copy
import math
import random
from pathlib import Path

from core.league_replay import local_file, read_json, finite
from core.league_visual_evaluation import (file_digest, new_labels, open_recording,
                                          save_json, validate_labels)


def binding(video, calibration):
    video, calibration = local_file(video), local_file(calibration)
    spec = read_json(calibration, 65536)
    references = list(spec.get("templates", {}).values())
    references += [p for paths in spec.get("viewport_nameplates", {}).values() for p in paths]
    references += [b["image"] for b in spec.get("blockers", [])]
    stat = video.stat()
    return {"video": str(video), "video_fingerprint": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
            "calibration": str(calibration), "calibration_sha256": file_digest(calibration),
            "reference_sha256": {str(local_file(calibration.parent / p)): file_digest(calibration.parent / p)
                                 for p in references}}


def prepare_review(video, calibration, output, *, windows=3, seconds=3., seed=64, exclude_labels=()):
    """Freeze one profile, randomly partition time windows BEFORE seeing predictions."""
    import cv2
    from core.league_visual import VisualCalibration
    profile = VisualCalibration.load(calibration)
    if type(windows) is not int or not finite(seconds) or not 1 <= windows <= 30 or not 1 <= seconds <= 30:
        raise ValueError("Use 1..30 windows per split and 1..30 seconds per window")
    cap = open_recording(video)
    try:
        fps, count = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or not 0 < count / fps <= 14400:
            raise ValueError("Unreadable local recording")
        duration = count / fps
    finally:
        cap.release()
    excluded = []
    for path in exclude_labels:
        value = validate_labels(read_json(path, 20_000_000))
        if local_file(value["video"]) != local_file(video):
            raise ValueError("Excluded reviews belong to another recording")
        coverage = value["coverage"]
        excluded += coverage.get("intervals", [[coverage["start_s"], coverage["end_s"]]])
    # A second between windows keeps adjacent episodes out of both partitions.
    candidates = [float(s) for s in range(1, max(1, int(duration - seconds)), math.ceil(seconds + 1))
                  if not any(s <= b + 1 and s + seconds >= a - 1 for a, b in excluded)]
    random.Random(seed).shuffle(candidates)
    if len(candidates) < windows * 2:
        raise ValueError("Not enough disjoint unreviewed footage for both splits")
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Choose a new review directory; existing reviews are never overwritten")
    output.mkdir(parents=True)
    plan = {"schema": 1, "kind": "blind_recording_review", **binding(video, calibration),
            "fps": fps/max(1,round(fps/min(10.,profile.fps,fps))), "seed": seed, "excluded_intervals": excluded, "windows": []}
    for i, start in enumerate(candidates[:windows * 2]):
        step=max(1,round(fps/plan["fps"]))
        first=math.ceil(start*fps/step)*step
        samples = [round(index/fps,6) for index in range(first,math.ceil((start+seconds)*fps),step)]
        plan["windows"].append({"id": f"window-{i + 1}", "split": "tuning" if i < windows else "heldout",
                                "samples_s": samples, "labels": f"window-{i + 1}-labels.json"})
    save_json(output / "plan.json", plan)
    return plan


def load_review(path):
    path = local_file(path)
    plan = read_json(path, 2_000_000)
    if plan.get("kind") != "blind_recording_review":
        raise ValueError("Use a blind recording review plan")
    current = binding(plan["video"], plan["calibration"])
    if any(plan.get(k) != v for k, v in current.items()):
        raise ValueError("Video, calibration or reference images changed after the review plan was frozen")
    windows = plan.get("windows", [])
    if not isinstance(windows, list) or not 2 <= len(windows) <= 60:
        raise ValueError("Invalid review windows")
    spans = [];paths=set();ids=set()
    for window in windows:
        samples = window.get("samples_s", [])
        if (window.get("split") not in ("tuning", "heldout") or not samples or len(samples) > 900
                or any(type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= 14400 for t in samples)
                or any(b <= a for a, b in zip(samples, samples[1:]))):
            raise ValueError("Invalid planned samples")
        target = (path.parent / window["labels"]).resolve()
        if target.parent != path.parent or target == path or target in paths:
            raise ValueError("Review labels must stay inside their review directory")
        if not isinstance(window.get("id"),str) or window["id"] in ids:raise ValueError("Duplicate review window IDs")
        paths.add(target);ids.add(window["id"])
        span = (samples[0], samples[-1])
        if any(span[0] <= b and span[1] >= a for a, b in spans):
            raise ValueError("Review windows overlap; held-out evaluation would leak tuning data")
        spans.append(span)
    return plan


def window_labels(plan_path, window_id):
    plan = load_review(plan_path)
    window = next((w for w in plan["windows"] if w["id"] == window_id), None)
    if window is None:
        raise ValueError("Unknown review window")
    value = new_labels(plan["video"], plan["calibration"])
    value.update(kind="real_player_view", review_plan_sha256=file_digest(plan_path),
                 calibration_sha256=plan["calibration_sha256"],
                 review_window=window_id, review_split=window["split"], reviewed_samples_s=[],
                 coverage={"start_s": window["samples_s"][0], "end_s": window["samples_s"][-1]},
                 reviewer={"kind": "unverified", "independent_human_review": False})
    return plan, window, value


def finalize_review(value, plan_path, window_id, *, human_confirmed=False):
    """Called only on an explicit operator action; never from detection."""
    plan, window, _ = window_labels(plan_path, window_id)
    value = validate_labels(copy.deepcopy(value))
    if (value.get("review_plan_sha256") != file_digest(plan_path) or value.get("review_window") != window_id
            or local_file(value["video"]) != local_file(plan["video"])
            or local_file(value["calibration"]) != local_file(plan["calibration"])):
        raise ValueError("Labels do not belong to this frozen review window")
    if value.get("reviews") or any("candidate_id" in l for l in value["labels"]):
        raise ValueError("Candidate confirmations are not blind independent labels")
    reviewed=value.get("reviewed_samples_s",[])
    if (not isinstance(reviewed,list) or any(not finite(t) for t in reviewed)
            or sorted(set(reviewed)) != window["samples_s"]):
        raise ValueError("Review every planned frame, including frames with no visible enemies")
    if value["kind"]!="real_player_view" or value["complete_for"]!=["minimap"]:
        raise ValueError("This blind review covers the real recorded minimap only")
    if any(not l.get("episode_id") for l in value["labels"]):
        raise ValueError("Give each independently observed continuous sighting a stable episode ID")
    if any(type(l.get("onset_known")) is not bool for l in value["labels"]):
        raise ValueError("Explicitly mark each onset as verified or unknown")
    if value["coverage"] != {"start_s": window["samples_s"][0], "end_s": window["samples_s"][-1]}:
        raise ValueError("Reviewed coverage must match the frozen window")
    if not human_confirmed:
        raise ValueError("An operator must explicitly confirm independent exhaustive review")
    value.update(annotation_complete=True,
                 reviewer={"kind": "operator_confirmed", "independent_human_review": True,
                           "method": "Blind sampled-frame review including absences; uncertain regions explicitly excluded"})
    return value


def score_review(plan_path, detections, split="heldout"):
    from core.league_visual_evaluation import evaluate_labels
    plan = load_review(plan_path)
    report = read_json(detections, 20_000_000)
    if (report.get("calibration_sha256") != plan["calibration_sha256"]
            or report.get("video_fingerprint") != plan["video_fingerprint"]
            or report.get("reference_sha256") != plan["reference_sha256"]):
        raise ValueError("Detections do not match the frozen review inputs")
    results = []
    for window in plan["windows"]:
        if window["split"] != split:
            continue
        path = Path(plan_path).resolve().parent / window["labels"]
        if not path.is_file():
            results.append({"window": window["id"], "status": "awaiting independent review"})
            continue
        labels = read_json(path, 20_000_000)
        reviewer=labels.get("reviewer",{})
        if (not isinstance(reviewer,dict) or reviewer.get("independent_human_review") is not True
                or reviewer.get("kind")!="operator_confirmed" or labels.get("annotation_complete") is not True):
            results.append({"window": window["id"], "status": "awaiting independent review"})
            continue
        finalize_review(labels, plan_path, window["id"], human_confirmed=True)
        results.append({"window": window["id"], **evaluate_labels(detections, path)})
    complete=bool(results) and all(r.get("accuracy") is not None for r in results)
    scores=[r["accuracy"] for r in results if r.get("accuracy") is not None]
    tp=sum(r["true_positive_frames"] for r in scores);fp=sum(r["false_positive_frames"] for r in scores)
    fn=sum(r["false_negative_frames"] for r in scores);duration=sum(r["evaluated_source_seconds"] for r in scores)
    false_episodes=sum(r["false_positive_episodes"] for r in scores)
    summary={"true_positive_frames":tp,"false_positive_frames":fp,"missed_positive_frames":fn,
             "precision":tp/(tp+fp) if tp+fp else None,"recall":tp/(tp+fn) if tp+fn else None,
             "false_positive_episodes":false_episodes,"reviewed_source_seconds":duration,
             "false_positive_episodes_per_minute":false_episodes*60/duration if duration else None,
             "missed_episodes":sum(r["missed_episodes"] for r in scores),
             "incorrect_identity_frames":sum(r["incorrect_identity_frames"] for r in scores)} if complete else None
    return {"split": split, "windows": results,"accuracy":summary,
            "independently_reviewed_sightings": sum((r.get("accuracy") or {}).get("labelled_episodes", 0) for r in results),
            "complete": complete}

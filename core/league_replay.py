"""Opt-in post-match PLAYER-VIEW video analysis. No live capture/API or LLM.

Template matching proposes visible-icon candidates. A human reviews thumbnails
before they enter the confirmed timeline. Missing icons never imply a position.
OpenCV is imported only for an explicit analysis/extraction operation.
"""
import argparse
import copy
import json
import math
from pathlib import Path

from core.cancellation import check_cancelled
from core.league_journal import stamp


class LocalFileError(ValueError):
    def __init__(self,path,purpose):
        self.path,self.purpose=path,purpose
        super().__init__(f"{purpose} does not exist or is not a regular file: {path}")


def local_file(value, *, purpose="Recorded file"):
    text = str(value)
    if "://" in text or text.startswith(("\\\\", "//")):
        raise ValueError("Use a local recorded file, not a stream, device or network path")
    path = Path(text).expanduser().resolve()
    if not path.is_file():
        raise LocalFileError(path,purpose)
    return path


def read_json(path, bound=2_000_000):
    path = local_file(path)
    if path.stat().st_size > bound:
        raise ValueError("Oversized replay configuration/report")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a replay object")
    return value


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def bounds(value):
    if (not isinstance(value, list) or len(value) != 4 or not all(finite(x) for x in value)
            or min(value) < 0 or min(value[2:]) <= 0 or value[0]+value[2] > 1 or value[1]+value[3] > 1):
        raise ValueError("Minimap/region bounds must be normalized [x,y,width,height] within the image")
    return tuple(value)


class MinimapDetector:
    def __init__(self, calibration, directory, *, allow_any_enemy=False, scale_factor=1.):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        if not finite(scale_factor) or not .5<=scale_factor<=2:
            raise ValueError("Invalid reference HUD scale")
        if calibration.get("perspective") != "player" or calibration.get("fog_of_war") != "player":
            raise ValueError("Use player-view footage with player fog of war; omniscient replay is not evidence of player visibility")
        self.box = bounds(calibration.get("minimap"))
        reference = calibration.get("reference_size")
        if (not isinstance(reference, list) or len(reference) != 2
                or any(type(n) is not int or not 100 <= n <= 7680 for n in reference)):
            raise ValueError("Provide reference_size [video_width, video_height] from calibration")
        self.reference = tuple(reference)
        # The editor/capture crop rounds each edge. Rounding the width instead
        # can add a pixel and stretch the saved reference before matching it.
        x,y,w,h=self.box
        self.width = round((x+w)*reference[0])-round(x*reference[0])
        self.height = round((y+h)*reference[1])-round(y*reference[1])
        if not 32 <= min(self.width, self.height) <= max(self.width, self.height) <= 1024:
            raise ValueError("Calibrated minimap must be 32..1024 pixels per side")
        roster = calibration.get("roster", [])
        if (not isinstance(roster, list) or not 2 <= len(roster) <= 20
                or any(not isinstance(p, dict) or not isinstance(p.get("champion"), str)
                       or not 1 <= len(p["champion"]) <= 80 or p.get("side") not in ("ally", "enemy") for p in roster)):
            raise ValueError("Provide a known champion roster with ally/enemy sides, without player identities")
        self.roster = tuple({"champion": p["champion"], "side": p["side"], "position": p.get("position", "")} for p in roster)
        target = calibration.get("target_champion")
        eligible = [p["champion"] for p in roster if p["side"] == "enemy" and (allow_any_enemy or p.get("position") == "JUNGLE")]
        self.target = target or (eligible[0] if len(eligible) == 1 else None)
        if self.target not in eligible:
            raise ValueError("A known enemy JUNGLE roster assignment is required; champion class does not determine the role")
        templates = calibration.get("templates", {})
        names = {p["champion"] for p in roster}
        if not isinstance(templates, dict) or set(templates) != names or len(names) != len(roster):
            raise ValueError("Cache one icon reference per roster champion, including allies; the offline analyzer can prepare missing official icons automatically")
        self.templates = {}
        for name, filename in templates.items():
            path = local_file(Path(directory)/str(filename))
            if path.stat().st_size > 1_000_000:
                raise ValueError("Oversized icon template")
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if (image is None or not 6 <= min(image.shape[:2]) <= max(image.shape[:2]) <= 128
                    or float(np.std(image.astype("float32"),axis=(0,1)).mean()) < 8):
                raise ValueError("Icon templates must be textured, tightly cropped BGR images, 6..128 pixels")
            self.templates[name] = image
        # Small, calibrated smoothing tolerates compressed/subpixel minimap
        # pixels. Off by default: a portrait alone is not a validated icon.
        self.blur_sigma = calibration.get("minimap_blur_sigma", 0.)
        if not finite(self.blur_sigma) or not 0 <= self.blur_sigma <= 1.2:
            raise ValueError("Minimap blur sigma must be 0..1.2 pixels")
        references = {name:self.prepare(image) for name,image in self.templates.items()}
        self._scaled = {}
        self._competitors = {}
        for name,image in references.items():
            for scale in (.9,1.,1.1):
                size = (round(image.shape[1]*scale*scale_factor),round(image.shape[0]*scale*scale_factor))
                resized = cv2.resize(image,size,interpolation=cv2.INTER_AREA)
                self._scaled[name,scale] = (resized,cv2.cvtColor(resized,cv2.COLOR_BGR2GRAY))
                if size not in self._competitors:
                    self._competitors[size] = {other:cv2.resize(icon,size) for other,icon in references.items()}
        # Same multi-channel zero-mean NCC as OpenCV, with template means and
        # norms cached. One small matrix product replaces nine repeated patch
        # normalizations/cv2 calls for roster ambiguity checks.
        self._competitor_names=tuple(references)
        self._competitor_vectors={}
        for size,icons in self._competitors.items():
            vectors=np.stack(list(icons.values())).astype(np.float32)
            vectors-=vectors.mean(axis=(1,2),keepdims=True)
            vectors=vectors.reshape(len(icons),-1)
            vectors/=np.maximum(np.linalg.norm(vectors,axis=1,keepdims=True),1e-10)
            self._competitor_vectors[size]=vectors
        self.threshold = calibration.get("threshold", .92)
        self.margin = calibration.get("margin", .08)
        if not finite(self.threshold) or not .8 <= self.threshold <= 1 or not finite(self.margin) or not .02 <= self.margin <= .5:
            raise ValueError("Use a threshold of .8..1 and ambiguity margin of .02...5")
        self.regions = []
        for region in calibration.get("regions", [])[:30]:
            if not isinstance(region, dict) or not isinstance(region.get("label"), str) or len(region["label"]) > 80:
                raise ValueError("Invalid calibrated region")
            self.regions.append((region["label"], bounds(region.get("bounds"))))

    def detect(self, frame):
        crop = self.crop(frame)
        return self.match(crop,self.target),crop

    def crop(self, frame):
        cv2, np = self.cv2, self.np
        height, width = frame.shape[:2]
        if abs((width/height)/(self.reference[0]/self.reference[1])-1) > .02:
            raise ValueError("Video aspect ratio changed; recalibrate the minimap")
        x,y,w,h = self.box
        crop = frame[round(y*height):round((y+h)*height), round(x*width):round((x+w)*width)]
        if crop.size == 0:
            raise ValueError("Empty calibrated minimap")
        return cv2.resize(crop, (self.width, self.height), interpolation=cv2.INTER_AREA)

    def match(self, crop, target, gray=None):
        cv2,np = self.cv2,self.np
        if target not in self.templates:
            return None
        # Passing gray means the caller already prepared this shared frame.
        if gray is None:
            crop = self.prepare(crop)
            gray = cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
        best = None
        for scale in (.9, 1., 1.1):
            reference,gray_reference = self._scaled[target,scale]
            th,tw = reference.shape[:2]
            if tw > self.width or th > self.height:
                continue
            score_map = cv2.matchTemplate(gray, gray_reference, cv2.TM_CCOEFF_NORMED)
            _, _, _, position = cv2.minMaxLoc(score_map)
            px,py = position
            score = float(cv2.matchTemplate(crop[py:py+th,px:px+tw],reference,cv2.TM_CCOEFF_NORMED)[0,0])
            if math.isfinite(score) and (best is None or score > best[0]):
                best = (score, position, tw, th)
        if best is None or best[0] < self.threshold:
            return None
        score, (px,py), tw,th = best
        patch = crop[py:py+th,px:px+tw]
        scores=self.roster_scores(patch)
        other=max(score for name,score in scores.items() if name!=target)
        if not math.isfinite(other) or score-other < self.margin:
            return None
        position = ((px+tw/2)/self.width, (py+th/2)/self.height)
        regions = [name for name,(x,y,w,h) in self.regions if x <= position[0] <= x+w and y <= position[1] <= y+h]
        region = regions[0] if len(regions) == 1 else f"minimap ({position[0]:.2f}, {position[1]:.2f})"
        return {"champion": target, "minimap_position": position, "region": region,
                "region_key": regions[0] if len(regions) == 1 else "unmapped",
                "template_similarity": round(score, 5), "ambiguity_margin": round(score-other, 5),
                "pixel_box": (px,py,tw,th)}

    def roster_scores(self,patch):
        array=patch.astype(self.np.float32)
        array-=array.mean(axis=(0,1),keepdims=True)
        array=array.ravel()
        norm=float(self.np.linalg.norm(array))
        if norm<=1e-10:return {name:0. for name in self._competitor_names}
        scores=self._competitor_vectors[patch.shape[1],patch.shape[0]]@array/norm
        return dict(zip(self._competitor_names,map(float,scores)))

    def prepare(self, crop):
        return self.cv2.GaussianBlur(crop,(0,0),self.blur_sigma) if self.blur_sigma else crop


class SightingTracker:
    """Two nearby visible samples before a candidate; no extrapolated samples."""
    def __init__(self):
        self.previous = None
        self.streak = 0
        self.last_region = None
        self.last_emit = -math.inf

    def update(self, detection, at):
        if detection is None:
            self.previous, self.streak, self.last_region = None, 0, None
            return None
        previous = self.previous
        near = previous is not None and at-previous[0] <= 1.5 and math.dist(
            detection["minimap_position"], previous[1]["minimap_position"]) <= .15 and detection["region_key"] == previous[1]["region_key"]
        self.streak = self.streak+1 if near else 1
        self.previous = (at, detection)
        if self.streak < 2 or detection["region_key"] == self.last_region or at-self.last_emit < 2:
            return None
        self.last_region, self.last_emit = detection["region_key"], at
        return {**previous[1], "pixel_box": detection["pixel_box"], "video_time_s": previous[0], "confirmed_visible_sample_s": at,
                "template_similarity": min(detection["template_similarity"],previous[1]["template_similarity"]),
                "uncertainty": "Visible-icon candidate, not a calibrated probability or proof of true unit identity; human review required",
                "source": "visible icon in supplied player-view recording; no fog-of-war inference"}


def analyze_video(video, calibration_path, output, cancellation=None, admit=None):
    import cv2
    video, calibration_path = local_file(video), local_file(calibration_path)
    calibration = read_json(calibration_path, 65536)
    detector = MinimapDetector(calibration, calibration_path.parent)
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output/"report.json").exists():
        raise ValueError("Choose a fresh analysis output directory so an old report cannot be mistaken for this run")
    cap = cv2.VideoCapture(str(video))
    original_threads = cv2.getNumThreads()
    cv2.setNumThreads(1)
    try:
        fps, total = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not cap.isOpened() or not math.isfinite(fps) or not .1 < fps <= 240 or not math.isfinite(total) or total <= 0 or total/fps > 14400:
            raise ValueError("Unsupported/unreadable recorded video; maximum duration is four hours")
        interval = .5  # Offline 2 Hz, independent of live voice/League workers.
        tracker, candidates = SightingTracker(), []
        analyzed = 0
        for index in range(0, int(total), max(1, round(fps*interval))):
            check_cancelled(cancellation)
            if admit:
                admit()  # Existing foreground policy can pause this worker.
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = cap.read()
            if not ok:
                raise ValueError("Video decode failed before the end; report discarded")
            if frame.shape[0] > 7680 or frame.shape[1] > 7680:
                raise ValueError("Recorded frame dimensions are too large")
            detection, crop = detector.detect(frame)
            analyzed += 1
            sighting = tracker.update(detection, index/fps)
            if sighting:
                if len(candidates) >= 1000:
                    raise ValueError("Too many candidates; review calibration before continuing")
                sighting["id"] = f"sighting-{len(candidates)+1:04d}"
                image_name = sighting["id"] + ".png"
                annotated = crop.copy()
                x,y,w,h = sighting.pop("pixel_box")
                cv2.rectangle(annotated,(x,y),(x+w,y+h),(0,0,255),1)
                if not cv2.imwrite(str(output/image_name),annotated):
                    raise ValueError("Could not save sighting evidence")
                sighting["evidence_image"] = image_name
                sighting["evidence_video_time_s"] = index/fps
                candidates.append(sighting)
        check_cancelled(cancellation)
        report = {"schema": 1, "source": "post-match player-view video", "video": video.name,
                  "perspective": "player", "fog_of_war": "player", "timestamps": "video seconds, not game clock",
                  "roster": detector.roster, "analyzed_frames": analyzed, "sample_interval_s": interval,
                  "candidates": candidates, "confirmed_sightings": [],
                  "limits": "No inference between visible samples. Visibility perspective/template identity require human calibration and review."}
        (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        return report
    finally:
        cap.release()
        cv2.setNumThreads(original_threads)


def load_report(path):
    report = read_json(path)
    if (report.get("schema") != 1 or report.get("source") != "post-match player-view video"
            or report.get("perspective") != "player" or report.get("fog_of_war") != "player"):
        raise ValueError("Not a Capri recorded-game report")
    candidates = report.get("candidates", [])
    confirmed = report.get("confirmed_sightings", [])
    if not isinstance(candidates,list) or len(candidates) > 1000 or not isinstance(confirmed,list) or len(confirmed)>len(candidates):
        raise ValueError("Invalid replay candidates")
    known = set()
    for item in candidates:
        if (not isinstance(item,dict) or not isinstance(item.get("id"),str) or item["id"] in known
                or not finite(item.get("video_time_s")) or item["video_time_s"] < 0
                or any(not isinstance(item.get(key),str) or not 1 <= len(item[key]) <= 200 for key in ("champion","region"))):
            raise ValueError("Invalid candidate fields")
        known.add(item["id"])
    reviewed_ids = set()
    for item in confirmed:
        if (not isinstance(item,dict) or item.get("id") not in known or item.get("review") != "user-confirmed visible icon"
                or item.get("id") in reviewed_ids
                or not any(all(item.get(key)==candidate.get(key) for key in ("id","champion","region","video_time_s")) for candidate in candidates)):
            raise ValueError("Confirmed sightings must match human-reviewed candidates")
        reviewed_ids.add(item["id"])
    return report


def review_report(path, confirmed_ids, missed_ids=()):
    report = load_report(path)
    candidates = report["candidates"]
    chosen, missed = set(confirmed_ids), set(missed_ids)
    known = {item.get("id") for item in candidates if isinstance(item,dict)}
    if not chosen <= known or not missed <= chosen:
        raise ValueError("Confirm only existing candidate IDs; missed sightings must also be confirmed")
    confirmed = []
    for item in candidates:
        if item.get("id") in chosen:
            if (not finite(item.get("video_time_s")) or item["video_time_s"] < 0
                    or not isinstance(item.get("champion"),str) or not isinstance(item.get("region"),str)):
                raise ValueError("Invalid candidate fields")
            confirmed.append({**copy.deepcopy(item), "review": "user-confirmed visible icon",
                              "player_marked_missed": item["id"] in missed})
    report["confirmed_sightings"] = sorted(confirmed,key=lambda item:item["video_time_s"])
    target = Path(path)
    target.write_text(json.dumps(report,indent=2),encoding="utf-8")
    return report


def timeline(report):
    return "\n".join(f"Video {stamp(item['video_time_s'])} — {item['champion']} icon visibly shown in {item['region']} (user confirmed)."
                     for item in report.get("confirmed_sightings", [])) or "No human-confirmed sightings; review the candidate evidence."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation",required=True)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("video",type=Path)
    analyze.add_argument("--calibration",type=Path,required=True)
    analyze.add_argument("--output",type=Path,required=True)
    analyze.add_argument("--post-match",action="store_true",required=True)
    review = sub.add_parser("review")
    review.add_argument("report",type=Path)
    review.add_argument("--confirm",nargs="*",default=[])
    review.add_argument("--missed",nargs="*",default=[])
    extract = sub.add_parser("frame")
    extract.add_argument("video",type=Path)
    extract.add_argument("--at",type=float,required=True)
    extract.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    if args.operation == "review":
        print(timeline(review_report(args.report,args.confirm,args.missed)))
    elif args.operation == "analyze":
        import config
        if not config.LEAGUE_REPLAY_ENABLED:
            parser.error("Explicitly enable CAPRI_LEAGUE_REPLAY=1 for offline analysis")
        report = analyze_video(args.video,args.calibration,args.output)
        print(f"Saved {len(report['candidates'])} candidates for review. None are confirmed automatically.")
    else:
        import cv2
        if not finite(args.at) or args.at < 0:
            parser.error("Use a nonnegative video timestamp")
        cap = cv2.VideoCapture(str(local_file(args.video)))
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC,args.at*1000)
            ok, frame = cap.read()
            if not ok or not cv2.imwrite(str(args.output),frame):
                parser.error("Could not extract the requested calibration frame")
        finally:
            cap.release()


if __name__ == "__main__":
    main()

"""Explicit local recording validation. Review labels are distinct from predictions."""
import json
import hashlib
import math
import time
from dataclasses import replace
from collections import Counter,defaultdict
from pathlib import Path
import config

from core.cancellation import check_cancelled
from core.league_metrics import LeagueMetrics
from core.league_replay import local_file, read_json, finite,bounds
from core.league_visual import VisualCalibration, LeagueVisualPipeline, VisualAlertDispatcher


def open_recording(path):
    import cv2
    path=local_file(path)
    if hasattr(cv2,"CAP_PROP_N_THREADS"):
        cap=cv2.VideoCapture(str(path),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,1])
        if cap.isOpened():return cap
        cap.release()
    return cv2.VideoCapture(str(path))


def save_json(path, value):
    path=Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    temporary.replace(path)
    return path


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def new_labels(video="", calibration=""):
    return {"schema":1,"kind":"unverified","video":str(video),"calibration":str(calibration),
            "coverage":{"start_s":0.,"end_s":0.},"annotation_complete":False,
            "complete_for":["minimap"],"labels":[],"reviews":[]}


def validate_labels(value):
    if not isinstance(value,dict) or value.get("kind") not in ("synthetic","real_player_view","unverified"):
        raise ValueError("Declare recording kind: synthetic, real_player_view or unverified")
    if type(value.get("annotation_complete")) is not bool:
        raise ValueError("Declare whether annotations are exhaustive")
    coverage=value.get("coverage",{})
    start,end=coverage.get("start_s"),coverage.get("end_s")
    if not finite(start) or not finite(end) or not 0<=start<=end<=14400:
        raise ValueError("Invalid labelled coverage interval")
    intervals=coverage.get("intervals",[[start,end]])
    if not isinstance(intervals,list) or not 1<=len(intervals)<=1000:
        raise ValueError("Invalid reviewed intervals")
    previous=-1
    for span in intervals:
        if (not isinstance(span,list) or len(span)!=2 or not all(finite(t) for t in span)
                or not start<=span[0]<=span[1]<=end or span[0]<=previous):
            raise ValueError("Reviewed intervals must be ordered, disjoint and inside coverage")
        previous=span[1]
    if not isinstance(value.get("complete_for"),list) or not set(value["complete_for"])<= {"minimap","viewport"}:
        raise ValueError("Unknown labelled detection source")
    if not isinstance(value.get("labels"),list) or len(value["labels"])>100000:
        raise ValueError("Invalid or oversized labels list")
    for item in value["labels"]:
        if not isinstance(item,dict) or not isinstance(item.get("champion"),str) or not 1<=len(item["champion"])<=80:
            raise ValueError("Each sighting needs a champion")
        a,b=item.get("start_s"),item.get("end_s")
        if not finite(a) or not finite(b) or not start<=a<=b<=end:
            raise ValueError("Sighting must lie within labelled coverage")
        if item.get("source") not in ("minimap","viewport"):
            raise ValueError("Sighting source must be minimap or viewport")
        p=item.get("position")
        if not isinstance(p,list) or len(p)!=2 or any(not finite(n) or not 0<=n<=1 for n in p):
            raise ValueError("Label the visible icon center in normalized region coordinates")
        if item.get("near_player") is not None and type(item["near_player"]) is not bool:
            raise ValueError("Proximity must be true, false or unknown")
        if "onset_known" in item and type(item["onset_known"]) is not bool:
            raise ValueError("onset_known must be boolean")
        if "episode_id" in item and (not isinstance(item["episode_id"],str) or not 1<=len(item["episode_id"])<=120):
            raise ValueError("Invalid sighting episode ID")
        if "onset_s" in item and (not finite(item["onset_s"]) or not start<=item["onset_s"]<=a):
            raise ValueError("An observed onset must be inside reviewed coverage and no later than its label")
    ignored=value.get("ignored_regions",[])
    if not isinstance(ignored,list) or len(ignored)>1000:raise ValueError("Invalid ignored audit regions")
    for region in ignored:
        if (not isinstance(region,dict) or region.get("source") not in ("minimap","viewport")
                or not finite(region.get("start_s")) or not finite(region.get("end_s"))
                or not start<=region["start_s"]<=region["end_s"]<=end):
            raise ValueError("Invalid ignored-region coverage")
        bounds(region.get("bounds"))
    reviews=value.get("reviews",[])
    if not isinstance(reviews,list) or len(reviews)>100000 or any(
            not isinstance(r,dict) or r.get("decision") not in ("confirmed","rejected")
            or not isinstance(r.get("candidate_id"),str) for r in reviews):
        raise ValueError("Invalid candidate review")
    return value


def draw_observations(frame, observations, profile):
    import cv2
    output=frame.copy()
    height,width=frame.shape[:2]
    for item in observations:
        roi=profile.minimap if item["source"]=="minimap" else profile.viewport
        if roi is None:continue
        x,y,w,h=item["box"]
        a=(round((roi[0]+x*roi[2])*width),round((roi[1]+y*roi[3])*height))
        b=(round((roi[0]+(x+w)*roi[2])*width),round((roi[1]+(y+h)*roi[3])*height))
        cv2.rectangle(output,a,b,(30,30,255),1)
        text=f'{item["champion"]} {item["confidence"]:.3f} t={item["timestamp"]:.2f}'
        cv2.putText(output,text,(a[0],max(12,a[1]-4)),cv2.FONT_HERSHEY_SIMPLEX,.4,(30,30,255),1)
    return output


class CandidateEpisodes:
    """Bounded episode summaries shared by recording and cached-frame evaluation."""
    def __init__(self,fps):
        self.gap=max(.35,2/fps)
        self.current={};self.candidates=[];self.omitted=0

    def update(self,items,at,index):
        current={}
        for item in items:
            key=(item["champion"],item["source"])
            previous=self.current.get(key)
            if previous and at-previous["last_seen_s"]<=self.gap and previous["location"]==item["location"]:
                previous["last_seen_s"]=at;previous["confirmed_samples"]+=1
            else:
                previous={**item,"id":f"candidate-{len(self.candidates)+1}","frame":index,"first_seen_s":item["first_seen_at"],
                          "confirmed_at_s":at,"last_seen_s":at,"confirmed_samples":1,"decision":"unreviewed"}
                if len(self.candidates)<5000:self.candidates.append(previous)
                else:self.omitted+=1
            current[key]=previous
        self.current=current


def analyze_recording(video, calibration, output, cancellation=None, admit=None, *, debug=False, knowledge=None, cache_minimap=False):
    """Sample a FILE, write observations JSONL and review candidates, not assertions."""
    video=local_file(video,purpose="Video recording")
    calibration=local_file(calibration,purpose="Calibration file")
    profile=VisualCalibration.load(calibration)
    lane=profile.offline_lane()
    if not lane.supported:
        raise ValueError("Declare game_mode CLASSIC and map_number 11; other modes are unsupported")
    if admit:admit()
    from core.league_icons import prepare_icons
    setup_started=time.perf_counter_ns()
    reference_setup=prepare_icons(calibration,cancellation,knowledge=knowledge)
    reference_setup_ms=(time.perf_counter_ns()-setup_started)/1e6
    if reference_setup["prepared"]:
        profile=VisualCalibration.load(calibration)
    import cv2
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    if any((output/name).exists() for name in ("detections.json","frames.jsonl","frames.partial.jsonl")):
        raise ValueError("Choose a fresh output directory to avoid confusing different runs")
    cap=open_recording(video)
    metrics=LeagueMetrics(True)
    old_threads=cv2.getNumThreads()
    cv2.setNumThreads(1)
    cpu_started=time.process_time();wall_started=time.perf_counter()
    analyzed=0;last_index=-1;peak_rss=None;last_time=-1
    pixels=times=None
    calibration_digest=file_digest(calibration)
    video_stat=video.stat()
    partial=output/"frames.partial.jsonl"
    try:
        fps,total=cap.get(cv2.CAP_PROP_FPS),cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not cap.isOpened() or not finite(fps) or not .1<fps<=240 or not finite(total) or not 0<total/fps<=14400:
            raise ValueError("Use a readable local recording of at most four hours")
        started=time.perf_counter_ns()
        pipeline=LeagueVisualPipeline(profile)
        metrics.finish("template_initialization",started)
        dispatcher=VisualAlertDispatcher(profile)
        dispatcher.muted=False  # Recorded proposals only. Never sent to live voice policy.
        actual_fps=min(10.,profile.fps,fps)
        step=max(1,round(fps/actual_fps))
        episodes=CandidateEpisodes(actual_fps)
        if cache_minimap:
            import numpy as np
            count=len(range(0,int(total),step))
            shape=(count,pipeline.minimap.height,pipeline.minimap.width,3)
            if math.prod(shape)+count*8>1024**3:
                raise ValueError("Explicit minimap cache exceeds the 1 GiB limit; use a shorter recording")
            pixels=np.lib.format.open_memmap(output/"minimap.partial.npy",mode="w+",dtype="uint8",shape=shape)
            times=np.lib.format.open_memmap(output/"times.partial.npy",mode="w+",dtype="float64",shape=(count,))
        with partial.open("w",encoding="utf-8") as stream:
            for index in range(0,int(total),step):
                check_cancelled(cancellation)
                if admit:admit()
                started=time.perf_counter_ns()
                # Sequential grab avoids a video seek/keyframe decode for every sample.
                while last_index<index:
                    if not cap.grab():raise ValueError("Video ended unexpectedly; partial report is not validation")
                    last_index+=1
                ok,frame=cap.retrieve()
                if not ok or max(frame.shape[:2])>7680:
                    raise ValueError("Unreadable or oversized recorded frame")
                metrics.finish("video_decode",started)
                pts=cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                at=pts if finite(pts) and 0<=pts<=14400 and pts>last_time else index/fps
                last_time=at
                started=time.perf_counter_ns()
                regions=profile.regions_from_frame(frame)
                observations=pipeline.process(regions,lane,at)
                metrics.finish("visual_processing",started)
                if pixels is not None:
                    cache_started=time.perf_counter_ns()
                    pixels[analyzed]=cv2.resize(regions["minimap"],(pipeline.minimap.width,pipeline.minimap.height),interpolation=cv2.INTER_AREA)
                    times[analyzed]=at
                    metrics.finish("explicit_minimap_cache_write",cache_started)
                started=time.perf_counter_ns()
                proposals=dispatcher.update(observations,lane.epoch,now=at)
                metrics.finish("confirmed_detection_to_notification_proposal",started)
                items=[o.to_dict() for o in observations]
                stream.write(json.dumps({"frame":index,"timestamp":at,"observations":items},allow_nan=False)+"\n")
                episodes.update(items,at,index)
                if debug and analyzed<500:
                    folder=output/"debug";folder.mkdir(exist_ok=True)
                    cv2.imwrite(str(folder/f"frame-{index:07}.jpg"),draw_observations(frame,items,profile))
                analyzed+=1
                if analyzed%50==0:
                    try:
                        import psutil
                        rss=psutil.Process().memory_info().rss
                        peak_rss=max(peak_rss or 0,rss)
                    except ImportError:pass
        cpu=time.process_time()-cpu_started;wall=time.perf_counter()-wall_started
        partial.replace(output/"frames.jsonl")
        report={"schema":1,"kind":"unverified","video":str(video),"calibration":str(calibration),
                "reference_setup":reference_setup,"reference_setup_ms":reference_setup_ms,
                "calibration_sha256":calibration_digest,"video_fingerprint":{"size":video_stat.st_size,"mtime_ns":video_stat.st_mtime_ns},
                "timestamp_method":"Decoder presentation timestamps when available; nominal frame time fallback",
                "duration_s":total/fps,"source_fps":fps,"analyzed_fps":fps/step,"analyzed_frames":analyzed,
                "intentionally_unsampled_frames":int(total)-analyzed,"dropped_frames":0,
                "frames":"frames.jsonl","candidates":episodes.candidates,"metrics":metrics.report(),
                "review_candidates_omitted_at_5000_limit":episodes.omitted,
                "cpu_seconds":cpu,"wall_seconds":wall,"cpu_percent_one_core":100*cpu/wall if wall else None,
                "sampled_peak_process_rss_bytes":peak_rss,"gpu":"CPU OpenCV; GPU utilization not measured",
                "accuracy":None,"accuracy_status":"Human ground truth required; candidates are not verified sightings",
                "notification_measurement":"Proposal construction only; no playback or physical audio measurement"}
        if pixels is not None:
            pixels.flush();times.flush()
            shape=list(pixels.shape)
            del pixels,times
            pixels=times=None
            (output/"minimap.partial.npy").replace(output/"minimap.npy")
            (output/"times.partial.npy").replace(output/"times.npy")
            report["minimap_cache"]={"pixels":"minimap.npy","timestamps":"times.npy","shape":shape,
                                      "reference_size":list(profile.reference),"minimap":list(profile.minimap),
                                      "privacy":"Explicit diagnostic cache only; never created by normal live operation"}
        save_json(output/"detections.json",report)
        return report
    finally:
        try:
            if pixels is not None:pixels.flush()
            if times is not None:times.flush()
        finally:
            del pixels,times
            cap.release()
            cv2.setNumThreads(old_threads)


def analyze_minimap_cache(source_report, calibration, output, cancellation=None, *, include_raw=False):
    """Repeat actual CV on identical lossless recorded pixels; excludes capture/decode."""
    import cv2
    import numpy as np
    source_path=local_file(source_report)
    source=read_json(source_path,20_000_000)
    cache=source.get("minimap_cache")
    if not isinstance(cache,dict):raise ValueError("Run detect with --cache-minimap before cached evaluation")
    calibration=local_file(calibration,purpose="Calibration file")
    profile=VisualCalibration.load(calibration)
    if list(profile.reference)!=cache["reference_size"] or list(profile.minimap)!=cache["minimap"]:
        raise ValueError("Cached pixels cannot be reinterpreted with another minimap geometry")
    if set(profile.rois())!={"minimap"}:raise ValueError("This lossless cache contains only minimap pixels")
    if (not finite(source.get("analyzed_fps")) or not 0<source["analyzed_fps"]<=30
            or not finite(source.get("source_fps")) or not 0<source["source_fps"]<=240):
        raise ValueError("Invalid recorded-pixel sampling cadence")
    fps=source["source_fps"]
    expected_fps=fps/max(1,round(fps/min(10.,profile.fps,fps)))
    if not math.isclose(expected_fps,source["analyzed_fps"],rel_tol=1e-6):
        raise ValueError("Cached sampling cadence differs; decode the recording again to change FPS")
    pixels=np.load(local_file(source_path.parent/cache["pixels"]),mmap_mode="r",allow_pickle=False)
    times=np.load(local_file(source_path.parent/cache["timestamps"]),mmap_mode="r",allow_pickle=False)
    x,y,w,h=profile.minimap
    shape=(round((y+h)*profile.reference[1])-round(y*profile.reference[1]),
           round((x+w)*profile.reference[0])-round(x*profile.reference[0]),3)
    if (pixels.dtype!=np.uint8 or list(pixels.shape)!=cache["shape"] or len(pixels.shape)!=4 or pixels.shape[1:]!=shape
            or pixels.shape[0]!=source.get("analyzed_frames")
            or pixels.nbytes>1024**3 or times.shape!=(pixels.shape[0],) or times.dtype!=np.float64
            or not np.isfinite(times).all() or not len(times) or times[0]<0 or np.any(np.diff(times)<=0)):
        raise ValueError("Malformed or oversized recorded-pixel cache")
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    if any((output/name).exists() for name in ("detections.json","frames.jsonl","frames.partial.jsonl")):
        raise ValueError("Use a fresh cached evaluation folder")
    old_threads=cv2.getNumThreads();cv2.setNumThreads(1)
    metrics=LeagueMetrics(True);lane=profile.offline_lane()
    started_cpu=time.process_time();started_wall=time.perf_counter()
    episodes=CandidateEpisodes(source["analyzed_fps"])
    partial=output/"frames.partial.jsonl"
    try:
        pipeline=LeagueVisualPipeline(profile)
        dispatcher=VisualAlertDispatcher(profile);dispatcher.muted=False
        with partial.open("w",encoding="utf-8") as stream:
            for i in range(len(times)):
                check_cancelled(cancellation)
                at=float(times[i]);index=round(at*source["source_fps"])
                started=time.perf_counter_ns()
                found=pipeline.process({"minimap":pixels[i]},lane,at)
                metrics.finish("visual_processing",started)
                started=time.perf_counter_ns();dispatcher.update(found,lane.epoch,now=at)
                metrics.finish("confirmed_detection_to_notification_proposal",started)
                items=[item.to_dict() for item in found]
                episodes.update(items,at,index)
                row={"frame":index,"timestamp":at,"observations":items}
                if include_raw:row["raw_observations"]=[item.to_dict() for item in pipeline.raw_observations]
                stream.write(json.dumps(row,allow_nan=False)+"\n")
        cpu=time.process_time()-started_cpu;wall=time.perf_counter()-started_wall
        partial.replace(output/"frames.jsonl")
        report={"schema":1,"kind":"unverified","video":source["video"],"calibration":str(calibration),
                "calibration_sha256":file_digest(calibration),"video_fingerprint":source["video_fingerprint"],
                "duration_s":source["duration_s"],"source_fps":source["source_fps"],"analyzed_fps":source["analyzed_fps"],
                "analyzed_frames":len(times),"frames":"frames.jsonl","candidates":episodes.candidates,
                "metrics":metrics.report(),"cpu_seconds":cpu,"wall_seconds":wall,"cpu_percent_one_core":100*cpu/wall,
                "accuracy":None,"accuracy_status":"Independent annotations required",
                "input":"Lossless real-recording minimap cache; excludes capture/decode, no model or audio",
                "source_report":str(source_path),"raw_observations_included":include_raw,
                "review_candidates_omitted_at_5000_limit":episodes.omitted}
        save_json(output/"detections.json",report)
        return report
    finally:
        cv2.setNumThreads(old_threads)
        del pixels,times


def compare_confirmation_policies(source_report,calibration,output,labels_paths=(),cancellation=None):
    """One actual CV pass, then identical raw evidence through each policy. No audio."""
    from core.league_visual import TemporalConfirmation,VisualObservation
    output=Path(output).resolve()
    for labels in labels_paths:validate_labels(read_json(labels,20_000_000))
    raw=analyze_minimap_cache(source_report,calibration,output/"raw",cancellation,include_raw=True)
    result={"kind":"Actual OpenCV on lossless recorded minimaps; confirmation replays exclude capture and audio",
            "raw_detection_metrics":raw["metrics"],"cpu_seconds":raw["cpu_seconds"],"wall_seconds":raw["wall_seconds"],"policies":{}}
    profile=VisualCalibration.load(calibration);lane=profile.offline_lane()
    for policy in ("strict3","two_validated","adaptive"):
        tracker=TemporalConfirmation(profile.fps,policy=policy);metrics=LeagueMetrics(True)
        folder=output/policy;folder.mkdir(parents=True,exist_ok=True)
        if (folder/"detections.json").exists():raise ValueError("Use a fresh policy comparison folder")
        episodes=CandidateEpisodes(raw["analyzed_fps"])
        with (output/"raw"/"frames.jsonl").open(encoding="utf-8") as source,(folder/"frames.jsonl").open("w",encoding="utf-8") as stream:
            for line in source:
                check_cancelled(cancellation)
                row=json.loads(line);at=row["timestamp"]
                observations=[VisualObservation(**item) for item in row["raw_observations"]]
                started=time.perf_counter_ns();found=tracker.update(observations,at,lane.epoch)
                metrics.finish("confirmation_policy_replay",started)
                row["observations"]=[item.to_dict() for item in found]
                episodes.update(row["observations"],at,row["frame"])
                stream.write(json.dumps(row,allow_nan=False)+"\n")
        report={**raw,"candidates":episodes.candidates,"frames":"frames.jsonl","confirmation_policy":policy,
                "policy_metrics":metrics.report(),"raw_source_report":str(output/"raw"/"detections.json")}
        path=save_json(folder/"detections.json",report)
        scores={Path(labels).stem:evaluate_labels(path,labels,allow_equivalent_calibration=True) for labels in labels_paths}
        result["policies"][policy]={"candidate_episodes":len(episodes.candidates),"policy_metrics":metrics.report(),"scores":scores}
        save_json(folder/"scores.json",scores)
    save_json(output/"comparison.json",result)
    return result


def simulate_visual_notifications(detections,labels_paths=(),*,baseline_factory=None,tts_delay=.4,busy_intervals=()):
    """Deterministic scheduling simulation. Never synthesizes, records or plays audio.

    Controlled-region eligibility is injected only in this simulation; it must
    never be interpreted as real lane/proximity evidence. Delay is an input,
    not a measured TTS duration. Actual profile eligibility is reported too.
    """
    from core.league_visual import VisualObservation
    from core.league_voice import LeagueVoicePolicy
    report=read_json(detections,20_000_000);profile=VisualCalibration.load(report["calibration"])
    labels=[validate_labels(read_json(path,20_000_000)) for path in labels_paths]
    if not finite(tts_delay) or not 0<=tts_delay<config.LEAGUE_VISION_ALERT_LIFETIME:
        raise ValueError("Simulated TTS delay must be shorter than the announcement lifetime")
    result={"measurement":"Simulated start/guard execution, NOT TTS synthesis, DAC output or audible speech",
            "simulated_tts_delay_s":tts_delay,"scenarios":{}}
    for legacy in ([True,False] if baseline_factory else [False]):
        for controlled in (False,True):
            clock=[0.];d=VisualAlertDispatcher(profile);d.muted=False
            policy=LeagueVoicePolicy(clock=lambda:clock[0],on_status=lambda a,s:d.lifecycle(a,s,clock[0]))
            if legacy:d,policy=baseline_factory(profile,clock)
            active=None;started_alerts=[];counts=Counter();stage_delays=LeagueMetrics(True)
            with local_file(Path(detections).parent/report["frames"]).open(encoding="utf-8") as source:
                for line in source:
                    row=json.loads(line);at=row["timestamp"];clock[0]=at
                    observations=[VisualObservation(**o) for o in row["observations"]]
                    if controlled:
                        observations=[replace(o,in_assigned_lane=True,location="controlled test region") for o in observations]
                    proposed=d.update(observations,1,now=at);counts["proposed"]+=len(proposed)
                    for ann in proposed:
                        if policy.offer(ann):counts["accepted"]+=1
                    busy=any(a<=at<=b for a,b in busy_intervals)
                    if active:
                        ann,due=active
                        if busy or not ann.is_fresh(at) or not d.is_current(ann,at):
                            counts["dropped_before_start"]+=1
                            if not legacy:policy.finish(ann)
                            active=None
                        elif at+.00051>=due:
                            original=d.offered[ann.event_key]
                            current=d.current[original.champion,original.source]
                            started_alerts.append({"timestamp":at,"champion":ann.champion,"source":current.source,
                                                   "position":list(current.position),"proposed_at":ann.captured_at})
                            counts["started"]+=1
                            stage_delays.record("simulated_proposal_to_start",round((at-ann.captured_at)*1e9))
                            if not legacy:policy.started(ann);policy.finish(ann)
                            active=None
                    item=policy.take(not busy and active is None,**({} if legacy else {"defer_start":True}))
                    if item:active=(item,at+tts_delay)
            scopes={}
            for path,truth in zip(labels_paths,labels):
                if not truth["annotation_complete"]:continue
                coverage=truth["coverage"];spans=coverage.get("intervals",[[coverage["start_s"],coverage["end_s"]]])
                nframes=0
                with local_file(Path(detections).parent/report["frames"]).open(encoding="utf-8") as source:
                    for line in source:
                        at=json.loads(line)["timestamp"]
                        nframes+=any(a-.00051<=at<=b+.00051 for a,b in spans)
                scoped=Counter();seen=set();onsets=LeagueMetrics(True)
                for alert in started_alerts:
                    at=alert["timestamp"]
                    if not any(a-.00051<=at<=b+.00051 for a,b in spans):continue
                    if truth.get("complete_champions") and alert["champion"] not in truth["complete_champions"]:continue
                    if alert["source"] not in truth["complete_for"]:continue
                    x,y=alert["position"]
                    if any(r["source"]==alert["source"] and r["start_s"]-.00051<=at<=r["end_s"]+.00051
                           and r["bounds"][0]<=x<=r["bounds"][0]+r["bounds"][2]
                           and r["bounds"][1]<=y<=r["bounds"][1]+r["bounds"][3] for r in truth.get("ignored_regions",[])):
                        scoped["excluded"]+=1;continue
                    match=next(((i,l) for i,l in enumerate(truth["labels"]) if l["champion"]==alert["champion"]
                                and l["source"]==alert["source"] and l["start_s"]-.00051<=at<=l["end_s"]+.00051
                                and math.dist(l["position"],alert["position"])<=.1),None)
                    if match:
                        i,label=match;key=(label["champion"],label["source"],label.get("episode_id",str(i)))
                        scoped["true_starts"]+=1;scoped["duplicates"]+=key in seen;seen.add(key)
                        if label.get("onset_known",True):
                            onsets.record("appearance_to_simulated_start",round((at-label.get("onset_s",label["start_s"]))*1e9))
                    else:scoped["false_starts"]+=1
                exposure=nframes/report["analyzed_fps"]
                scopes[Path(path).stem]={"true_starts":scoped["true_starts"],"false_starts":scoped["false_starts"],
                    "duplicates":scoped["duplicates"],"excluded":scoped["excluded"],"reviewed_seconds":exposure,
                    "simulated_false_starts_per_minute":scoped["false_starts"]*60/exposure if exposure else None,
                    "latency":onsets.report(),"annotation_provenance":truth.get("reviewer")}
            name=("baseline" if legacy else "optimized")+("_controlled_region" if controlled else "_actual_eligibility")
            result["scenarios"][name]={"counts":dict(counts),"scoped_scores":scopes,"stage_delays":stage_delays.report(),
                                       "started_alerts":started_alerts,"busy_intervals":list(busy_intervals)}
    return result


def evaluate_labels(detections_path, labels_path, *, allow_equivalent_calibration=False):
    """Frame-level scoring within explicit exhaustive coverage, positions matched before identity."""
    report=read_json(detections_path,20_000_000)
    labels=validate_labels(read_json(labels_path,20_000_000))
    if local_file(labels["video"])!=local_file(report["video"]):
        raise ValueError("Labels and detections refer to different recordings")
    if local_file(labels["calibration"])!=local_file(report["calibration"]):
        before=VisualCalibration.load(labels["calibration"])
        after=VisualCalibration.load(report["calibration"])
        if not allow_equivalent_calibration or (before.reference,before.minimap,before.viewport)!=(after.reference,after.minimap,after.viewport):
            raise ValueError("Labels and detections use different calibrations; only identical geometry can share annotations")
    if report.get("calibration_sha256") and file_digest(report["calibration"])!=report["calibration_sha256"]:
        raise ValueError("Calibration changed after detection. Regenerate candidates before scoring")
    stat=local_file(report["video"]).stat()
    if report.get("video_fingerprint") and report["video_fingerprint"]!={"size":stat.st_size,"mtime_ns":stat.st_mtime_ns}:
        raise ValueError("Recording changed after detection. Regenerate candidates before scoring")
    decisions={r["candidate_id"]:r["decision"] for r in labels["reviews"]}
    candidate_ids={c["id"] for c in report["candidates"]}
    if not decisions.keys()<=candidate_ids:raise ValueError("Review contains unknown candidate IDs")
    result={"recording_kind":labels["kind"],"real_footage_accuracy":None,
            "annotation_provenance":labels.get("reviewer","operator-supplied; independent review not established"),
            "reviewed_candidates":len(decisions),"confirmed_candidates":sum(d=="confirmed" for d in decisions.values()),
            "rejected_candidates":sum(d=="rejected" for d in decisions.values()),"processing_metrics":report["metrics"],
            "cpu_seconds":report["cpu_seconds"],"wall_seconds":report["wall_seconds"],
            "sampled_peak_process_rss_bytes":report.get("sampled_peak_process_rss_bytes"),
            "method":"Frame-level matching within 0.10 normalized region distance; identity evaluated after spatial association"}
    if not labels["annotation_complete"] or not labels["complete_for"] or labels["coverage"]["end_s"]<=labels["coverage"]["start_s"]:
        result.update(accuracy=None,status="Confirm/reject reviews alone do not measure recall. Label missed positives and declare exhaustive coverage.")
        return result
    tp=fp=fn=identity_total=identity_correct=proximity_total=proximity_correct=frames=0
    first_detections={};first_raw={};false_episodes={};false_count=0;latency=LeagueMetrics(True)
    per_champion=defaultdict(Counter)
    start,end=labels["coverage"]["start_s"],labels["coverage"]["end_s"]
    intervals=labels["coverage"].get("intervals",[[start,end]])
    def episode(index,label):return (label["champion"],label["source"],label.get("episode_id",str(index)))
    onsets={}
    for i,label in enumerate(labels["labels"]):
        if not label.get("onset_known",True):continue
        key=episode(i,label);onset=label.get("onset_s",label["start_s"])
        if not any(a-.00051<=onset<=b+.00051 for a,b in intervals):raise ValueError("Known onset is outside reviewed intervals")
        if key in onsets and abs(onsets[key]-onset)>.00051:raise ValueError("Conflicting onset times for the same sighting episode")
        onsets[key]=onset
    ignored_predictions=0
    declared=labels.get("complete_champions",[])
    if not isinstance(declared,list) or any(not isinstance(n,str) for n in declared):raise ValueError("Invalid complete_champions")
    if declared and any(l["champion"] not in declared for l in labels["labels"]):
        raise ValueError("Sighting label is outside the declared champion review scope")
    for name in declared:per_champion[name]
    path=local_file(Path(detections_path).parent/report["frames"])
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            frame=json.loads(line);at=frame["timestamp"]
            window=next((i for i,(a,b) in enumerate(intervals) if a-.00051<=at<=b+.00051),None)
            if window is None:continue
            frames+=1
            ignored=[r for r in labels.get("ignored_regions",[]) if r["start_s"]-.00051<=at<=r["end_s"]+.00051]
            def excluded(item):
                px,py=item["position"]
                return any(r["source"]==item["source"] and r["bounds"][0]<=px<=r["bounds"][0]+r["bounds"][2]
                           and r["bounds"][1]<=py<=r["bounds"][1]+r["bounds"][3] for r in ignored)
            expected=[(i,l) for i,l in enumerate(labels["labels"]) if l["source"] in labels["complete_for"]
                      and l["start_s"]-.00051<=at<=l["end_s"]+.00051 and not excluded(l)]
            for i,label in expected:
                key=episode(i,label)
                if key in first_raw:continue
                if any(o["champion"]==label["champion"] and o["source"]==label["source"]
                       and not excluded(o) and math.dist(o["position"],label["position"])<=.1 for o in frame.get("raw_observations",[])):
                    first_raw[key]=at
                    if key in onsets:latency.record("appearance_to_first_raw_detection",round((at-onsets[key])*1e9))
            present=[o for o in frame["observations"] if o["source"] in labels["complete_for"]
                     and (not declared or o["champion"] in declared)]
            observed=[o for o in present if not excluded(o)]
            ignored_predictions+=len(present)-len(observed)
            pairs=sorted((math.dist(o["position"],l["position"]),oi,i,l)
                         for oi,o in enumerate(observed) for i,l in expected if o["source"]==l["source"])
            used_o=set();used_l=set();wrong=[]
            for distance,oi,i,label in pairs:
                if distance>.1 or oi in used_o or i in used_l:continue
                used_o.add(oi);used_l.add(i);item=observed[oi]
                identity_total+=1
                if item["champion"]==label["champion"]:
                    tp+=1;identity_correct+=1
                    per_champion[label["champion"]]["tp"]+=1
                    key=episode(i,label)
                    if key not in first_detections:
                        first_detections[key]=at
                        if key in onsets:
                            latency.record("appearance_to_temporally_confirmed_sighting",round((at-onsets[key])*1e9))
                            if key in first_raw:latency.record("first_raw_to_confirmation",round((at-first_raw[key])*1e9))
                else:
                    fp+=1;fn+=1;wrong.append(item)
                    per_champion[item["champion"]]["fp"]+=1;per_champion[label["champion"]]["fn"]+=1
                if item["champion"]==label["champion"] and label.get("near_player") is not None and item.get("near_player") is not None:
                    proximity_total+=1;proximity_correct+=item["near_player"]==label["near_player"]
            unmatched=[o for i,o in enumerate(observed) if i not in used_o]
            fp+=len(unmatched);fn+=len(expected)-len(used_l)
            for item in unmatched:per_champion[item["champion"]]["fp"]+=1
            for i,label in expected:
                if i not in used_l:per_champion[label["champion"]]["fn"]+=1
            # Episode count uses false observations, not every repeated frame.
            for item in unmatched+wrong:
                key=(window,item["source"],item["champion"])
                if at-false_episodes.get(key,-math.inf)>max(.5,2/report["analyzed_fps"]):
                    false_count+=1;per_champion[item["champion"]]["false_episodes"]+=1
                false_episodes[key]=at
    if frames==0:raise ValueError("No analyzed frames within labelled coverage")
    duration=frames/report["analyzed_fps"]
    detail={name:{"true_positive_frames":c["tp"],"false_positive_frames":c["fp"],"missed_positive_frames":c["fn"],
                  "labelled_positive_frames":c["tp"]+c["fn"],"precision":c["tp"]/(c["tp"]+c["fp"]) if c["tp"]+c["fp"] else None,
                  "recall":c["tp"]/(c["tp"]+c["fn"]) if c["tp"]+c["fn"] else None,
                  "false_positive_episodes":c["false_episodes"]} for name,c in per_champion.items()}
    accuracy={"true_positive_frames":tp,"false_positive_frames":fp,"false_negative_frames":fn,"evaluated_frames":frames,
              "recall":tp/(tp+fn) if tp+fn else None,"precision":tp/(tp+fp) if tp+fp else None,
              "false_positive_episodes":false_count,"false_positives_per_minute":false_count*60/duration,
              "evaluated_source_seconds":duration,"reviewed_intervals":intervals,"reviewed_champions":declared or "all",
              "false_positive_rate_kind":"Candidate sighting episodes, not spoken alerts; excluded regions are not scored",
              "per_champion":detail,
              "ignored_prediction_frames":ignored_predictions,"ignored_regions":labels.get("ignored_regions",[]),
              "champion_identity_accuracy":identity_correct/identity_total if identity_total else None,
              "identity_associations":identity_total,"proximity_accuracy":proximity_correct/proximity_total if proximity_total else None,
              "proximity_comparisons":proximity_total,"detected_label_intervals":len(first_detections),
              "latency":latency.report(),"known_onset_episodes":len(onsets),
              "latency_note":"Only explicitly known onsets contribute; left-censored sightings cannot establish appearance latency"}
    result.update(accuracy=accuracy,status="Scored only within declared review scope; consult annotation_provenance. Not independent certification.")
    if labels["kind"]=="real_player_view":result["real_footage_accuracy"]=accuracy
    return result

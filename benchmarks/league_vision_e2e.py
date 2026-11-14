"""Explicit, offline-only paced vision -> existing AdviceWorker -> real audio.

Never starts League services or changes their approval flags. Audio and passive
loopback require CLI opt-in. Frames and speech content are not written here.
"""
import csv
import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass,replace
from pathlib import Path
from threading import Event,RLock
from types import SimpleNamespace

import config
from core.interaction import InteractionState
from core.latency import LatencyTrace,VoiceMetrics
from core.league_replay import local_file,read_json,finite
from core.league_visual import VisualCalibration,LeagueVisualPipeline,VisualAlertDispatcher
from core.league_voice import LeagueVoicePolicy
from core.league_visual_evaluation import open_recording,file_digest,save_json,validate_labels


SCENARIOS={
    "verified":(0.,3.,"",None),  # Filled only from an explicit operator whitelist.
    "warwick":(213.9,3.6,"Warwick",214.4),
    "galio":(4.8,2.3,"Galio",5.2),
    "overlap":(18.4,2.,"Darius",None),
    "two-enemies":(7.9,3.2,"Karma",8.4),
    "synthetic":(0.,4.5,"Warwick",.4),
    "continuous":(0.,12.,"Warwick",.4),
    "disappear":(0.,2.,"Warwick",.4),
    "new-match":(0.,2.,"Warwick",.4),
    "already-speaking":(0.,4.5,"Warwick",.4),
    "interrupt":(0.,4.5,"Warwick",.4),
}
SYNTHETIC=frozenset(("synthetic","continuous","disappear","new-match","already-speaking","interrupt"))
STAGES=("T0","T1","T2","T3","T4","T5","T6","T7","T8","T9","T10","T11")


def verified_clip(eligibility,index=0):
    if not eligibility.spec or eligibility.synthetic:
        raise ValueError("--scenarios verified requires an operator-confirmed manifest and verified eligibility")
    labels=eligibility.spec["selected_sightings"]
    if not 0<=index<len(labels):raise ValueError("Verified sighting index is outside the selected whitelist")
    label=labels[index];start=max(0.,label["start_s"]-.5)
    duration=min(6.,label["end_s"]-start+.5)
    onset=label.get("onset_s",label["start_s"]) if label.get("onset_known") is True else None
    # A left-censored interval can authorize current visibility, never appearance timing.
    return (start,duration,label["champion"],onset)


def fingerprint(video):
    stat=local_file(video).stat()
    return {"size":stat.st_size,"mtime_ns":stat.st_mtime_ns}


def ensure_voice_ready(warm_up,session):
    """Reuse the native worker if alive, account for recovery outside warm trials."""
    start=time.perf_counter_ns()
    before=session.process.pid if session.process and session.process.poll() is None else None
    warm_up()
    after=session.process.pid if session.process and session.process.poll() is None else None
    if after is None:raise RuntimeError("Warm trials require the existing persistent Kokoro worker to be ready")
    return {"before_pid":before,"after_pid":after,"check_ms":(time.perf_counter_ns()-start)/1e6,
            "outside_timed_source":True,"reloaded":before!=after}


def distribution(values,missing=0):
    values=sorted(values)
    if not values:return {"count":0,"missing":missing}
    return {"count":len(values),"missing":missing,**{f"p{p}_ms":values[int((len(values)-1)*p/100)] for p in (50,95,99)},
            "max_ms":values[-1],"variance_ms2":statistics.pvariance(values)}


def attach_loopback(row,observed):
    """A quiet mix onset is attributable only after this reply's PCM/callback.

    This temporal check is necessary, not a substitute for keeping other audio
    quiet or selecting the correct endpoint. Missing output stays missing.
    """
    if observed:
        if (row.get("T9") is not None and row.get("T10") is not None
                and min(observed)>=row["T10"]):
            row["T11"]=observed[0];row["loopback_received_ns"]=observed[1]
        else:row["loopback_error"]="Mix onset cannot be attributed to this utterance"
    elif row.get("T10") is not None:row["loopback_error"]="No matching endpoint signal observed"


class Eligibility:
    """Verified whitelist is independent of detections and of attached markers."""
    def __init__(self,video,calibration,manifest=None,synthetic=False):
        self.synthetic=synthetic;self.spec=None
        if manifest and not synthetic:
            value=read_json(manifest,2_000_000)
            if (not isinstance(value,dict) or value.get("kind")!="operator_confirmed_replay_eligibility"
                    or value.get("video_fingerprint")!=fingerprint(video)
                    or value.get("calibration_sha256")!=file_digest(calibration)
                    or Path(value.get("video","")).resolve()!=local_file(video)):
                raise ValueError("Eligibility must be manually confirmed for this unchanged local recording/calibration")
            if not isinstance(value.get("selected_sightings"),list) or not 1<=len(value["selected_sightings"])<=1000:
                raise ValueError("Select a bounded list of visually verified sightings")
            if not isinstance(value.get("reviewer"),dict) or value["reviewer"].get("kind")!="operator_confirmed":
                raise ValueError("Eligibility requires explicit operator review")
            validate_labels({"kind":"real_player_view","annotation_complete":False,
                "coverage":{"start_s":0,"end_s":14400},"complete_for":["minimap","viewport"],
                "labels":value["selected_sightings"]})
            self.spec=value

    def select(self,observations,video_s,scenario,profile):
        if self.synthetic:
            name=SCENARIOS[scenario][2]
            # Only the explicitly selected test champion. No fabricated distance
            # and no location-specific test speech. Never used by live services.
            return tuple(replace(o,in_assigned_lane=True,location="offline test region") for o in observations if o.champion==name)
        if self.spec is None:return ()
        result=[]
        for o in observations:
            for label in self.spec["selected_sightings"]:
                if (o.champion==label["champion"] and o.source==label["source"]
                        and label["start_s"]-.00051<=video_s<=label["end_s"]+.00051
                        and math.dist(o.position,label["position"])<=.1
                        and (o.in_assigned_lane is True or o.source=="viewport" and profile.viewport_enabled)):
                    result.append(o);break
        return tuple(result)


@dataclass
class ReplayFrame:
    video_s:float
    regions:dict
    full:object=None


def load_clip(video,profile,scenario,*,full=False,max_bytes=384*1024*1024):
    """Bounded predecode, then release samples at their source-time cadence.

    Video audio is never opened. Decode time is reported separately, not called
    Windows screen-capture time. Mode A stores only configured cropped ROIs.
    """
    import cv2,numpy as np
    start,duration,champion,onset=SCENARIOS[scenario];began=time.perf_counter_ns();frames=[];used=0
    if scenario in SYNTHETIC:
        from core.league_replay import MinimapDetector
        detector=MinimapDetector({**profile.spec,"target_champion":champion},profile.directory,allow_any_enemy=True)
        blank=np.zeros((profile.reference[1],profile.reference[0],3),np.uint8)
        visible=blank.copy();x,y,w,h=profile.minimap
        ox,oy=round(x*profile.reference[0])+40,round(y*profile.reference[1])+40
        icon=detector.templates[champion];ih,iw=icon.shape[:2];visible[oy:oy+ih,ox:ox+iw]=icon
        rois=[profile.regions_from_frame(image) for image in (blank,visible)]
        for i in range(round(duration*profile.fps)):
            at=i/profile.fps
            present=at>=onset and (scenario!="disappear" or at<.7)
            frames.append(ReplayFrame(at,rois[int(present)],(visible if present else blank) if full else None))
        return frames,{"kind":"synthetic reference icon fixture; no real-footage accuracy","decode_ms":0,"source_fps":profile.fps}
    cap=open_recording(video)
    try:
        fps=cap.get(cv2.CAP_PROP_FPS)
        if not cap.isOpened() or not finite(fps) or fps<=0:raise ValueError("Unreadable local recording")
        first=round(start*fps);step=max(1,round(fps/profile.fps));cap.set(cv2.CAP_PROP_POS_FRAMES,first)
        last=first-1
        for index in range(first,round((start+duration)*fps),step):
            while last<index:
                if not cap.grab():raise ValueError("Recording ended during test clip")
                last+=1
            ok,image=cap.retrieve()
            if not ok:raise ValueError("Failed to decode recorded frame")
            regions=profile.regions_from_frame(image)
            # Crops must own bounded storage; otherwise small views retain full frames.
            regions={n:roi.copy() for n,roi in regions.items()}
            pts=cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            at=pts if finite(pts) and abs(pts-index/fps)<.1 else index/fps
            used+=sum(im.nbytes for im in regions.values())+(image.nbytes if full else 0)
            if used>max_bytes:raise ValueError("Clip exceeds bounded predecode memory; choose a shorter clip")
            frames.append(ReplayFrame(at,regions,image if full else None))
        return frames,{"kind":"real recorded player-view pixels","source_fps":fps,"analysis_fps":fps/step,
                       "normal_speed":True,"predecoded_bytes":used,"decode_ms":(time.perf_counter_ns()-began)/1e6}
    finally:cap.release()


class Resources:
    def __init__(self):
        import psutil
        self.psutil=psutil;self.process=psutil.Process();self.start=time.perf_counter();self.last=0
        self.initial={};self.peak=0;self.samples=0;self.max_queue=0;self.gpu=[]
        self.sample(force=True)

    def sample(self,force=False):
        now=time.perf_counter()
        if not force and now-self.last<.25:return
        self.last=now;self.samples+=1;rss=0
        for process in [self.process]+self.process.children(recursive=True):
            try:
                cpu=process.cpu_times();pid=process.pid
                self.initial.setdefault(pid,(cpu.user+cpu.system,cpu.user+cpu.system))
                self.initial[pid]=(self.initial[pid][0],cpu.user+cpu.system)
                rss+=process.memory_info().rss
            except self.psutil.Error:pass
        self.peak=max(self.peak,rss)

    def report(self):
        self.sample(force=True);wall=time.perf_counter()-self.start
        cpu=sum(end-start for start,end in self.initial.values())
        return {"wall_s":wall,"process_tree_cpu_s":cpu,"cpu_percent_one_core":100*cpu/wall,
                "peak_sampled_process_tree_rss_bytes":self.peak,"samples":self.samples,
                "gpu":"Unavailable unless external NVIDIA workload sampling is requested",
                "capture_queue_depth_max":0,"queue_model":"Synchronous latest-frame sampling; stale source samples dropped"}


def summarize(rows):
    result={}
    for policy in ("strict3","adaptive"):
        cases={}
        for scenario in sorted({r["scenario"] for r in rows}):
            selected=[r for r in rows if r["policy"]==policy and r["scenario"]==scenario and not r.get("warmup")]
            per_trial={}
            for row in selected:
                if row.get("is_target"):
                    per_trial.setdefault(row["trial"],[]).append(row)
            samples=[]
            for values in per_trial.values():
                spoken=[r for r in values if r.get("T10") is not None]
                samples.append(min(spoken,key=lambda r:r["T10"]) if spoken else values[0])
            timings={}
            for name,a,b in (("appearance_to_confirmation","T0","T5"),("confirmation_to_loopback","T5","T11"),
                ("capture_to_loopback","T1","T11"),("appearance_to_loopback","T0","T11"),
                ("confirmation_to_dac_estimate","T5","dac_ns"),("capture_to_dac_estimate","T1","dac_ns"),
                ("capture_to_pcm_ready","T1","T9"),("tts_to_pcm_ready","T8","T9")):
                values=[(r[b]-r[a])/1e6 for r in samples if r.get(a) is not None and r.get(b) is not None]
                timings[name]=distribution(values,len(samples)-len(values))
            cases[scenario]={"episode_rows":len(selected),"paired_trials":len(samples),
                "statuses":{s:sum(r["status"]==s for r in selected) for s in sorted({r["status"] for r in selected})},"timings":timings}
        result[policy]=cases
    return result


def run_trial(frames,profile,eligibility,scenario,policy,trial,*,audio=False,probe=None,window=None,warmup=False,observation_sink=None):
    """Paced source and real application objects. One existing AdviceWorker at a time."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from gui.workers import AdviceWorker
    app=QApplication.instance();lock=RLock();epoch=trial+1
    profile=VisualCalibration({**profile.spec,"confirmation_policy":policy},profile.directory)
    pipeline=LeagueVisualPipeline(profile);dispatcher=VisualAlertDispatcher(profile);dispatcher.muted=False
    metrics=VoiceMetrics(True);interaction=InteractionState();rows={};extra_rows=[];notification_rows={};frame_stamps={};active=None
    if not frames:raise ValueError("The replay has no frames")
    counter=0;source_start=frames[0].video_s;stats={"frames":0,"dropped_frames":0,"stale_frames":0,"processing_ms":[],"capture_ms":[],"capture_to_proposal_ms":[]}
    resources=Resources();injected=False;injection_ns=None;suppress=False
    origin=time.perf_counter_ns();waiter=Event()

    def current(ann):
        if not ann.event_key.startswith("visual:"):return True
        with lock:return dispatcher.is_current(ann)

    def status(ann,state):
        with lock:
            dispatcher.lifecycle(ann,state)
            row=notification_rows.get(ann.event_key)
            if row:
                if state=="accepted":row["T7"]=ann.accepted_ns
                if state=="started":row["status"]="started"
                if state=="dropped" and row["status"]!="started":row["status"]="dropped"

    league=SimpleNamespace(announcement_current=current,announcement_status=status,metrics=metrics)
    context=SimpleNamespace(league=league,interaction=interaction,spatial=None)
    voice_policy=LeagueVoicePolicy(on_status=status)

    def start_speech(ann,row):
        nonlocal active
        trace=LatencyTrace(metrics)
        row["loopback_error"]=None
        if probe:
            try:probe.arm()
            except RuntimeError as error:row["loopback_error"]=str(error)
        # The busy fixture represents an already active user reply. Its own
        # existing speech worker may speak, while the main interaction state
        # suppresses lower-priority sighting notices. No second worker is run.
        worker_context=(SimpleNamespace(league=league,interaction=InteractionState(),spatial=None)
                        if ann.event_key=="benchmark:busy" else context)
        worker=AdviceWorker([],worker_context,ann,trace=trace)
        def started(item):
            if item.event_key.startswith("visual:"):
                with lock:voice_policy.started(item)
        worker.advice_started.connect(started,Qt.ConnectionType.DirectConnection)
        errors=[];worker.advice_error.connect(errors.append,Qt.ConnectionType.DirectConnection)
        active=(worker,ann,row,trace,errors)
        worker.start()

    def finish_speech(force=False):
        nonlocal active
        if active is None:return
        worker,ann,row,trace,errors=active
        if force and worker.isRunning():
            row.setdefault("cancellation_reason","source_or_benchmark_end");worker.stop()
        if worker.isRunning():
            if not force:return
            if not worker.wait(5000):raise RuntimeError("Existing speech worker did not shut down")
        worker.wait();marks=trace.snapshot()
        row.update(T8=marks.get("tts_submitted"),T9=marks.get("audio_ready"),T10=marks.get("device_submitted"),
                   dac_ns=marks.get("device_playback"),native_pcm_ns=marks.get("native_pcm_ready"),
                   native_callback_ns=marks.get("native_first_callback"),worker_finished_ns=time.perf_counter_ns(),
                   playback_queued_ns=marks.get("playback_queued"),output_open_requested_ns=marks.get("output_open_requested"),
                   output_opened_ns=marks.get("output_opened"),output_underflows=metrics.report().get("counters",{}).get("output_underflows",0),
                   interruption_ns=marks.get("interruption_detected"),abort_complete_ns=marks.get("output_abort_complete"))
        row["outcome"]=(row.get("cancellation_reason") or "interrupted") if row.get("interruption_ns") else "cancelled_or_stale" if ann.cancel_event and ann.cancel_event.is_set() else "completed"
        if errors:row["error"]=errors[0];row["status"]="speech_error"
        elif row.get("T10") is not None:row["status"]="started"
        elif row["status"]!="not_eligible":row["status"]="dropped_before_output"
        if probe and not row.get("loopback_error"):
            try:
                attach_loopback(row,probe.result(0))
            except RuntimeError as error:row["loopback_error"]=str(error)
        with lock:voice_policy.finish(ann)
        if ann.event_key=="benchmark:busy":interaction.finish_user_turn()
        worker.deleteLater();active=None

    try:
        if scenario=="already-speaking" and audio:
            ann=__import__("core.league_voice",fromlist=["LeagueAnnouncement"]).LeagueAnnouncement(
                "Benchmark test speech. Capri is already speaking.","benchmark:busy",time.monotonic(),10)
            interaction.begin_user_turn()
            busy_row={"status":"busy_fixture","scenario":scenario,"policy":policy,"trial":trial,"warmup":warmup,
                      "episode_id":f"{scenario}-{policy}-{trial}-busy","notification_id":"benchmark:busy","epoch":epoch,
                      "eligibility":"synthetic_busy_speech","is_target":False}
            extra_rows.append(busy_row);start_speech(ann,busy_row)
        index=0
        while index<len(frames):
            app.processEvents();finish_speech();resources.sample()
            due=origin+int((frames[index].video_s-source_start)*1e9)
            now=time.perf_counter_ns()
            if now<due:
                waiter.wait(min(.005,(due-now)/1e9));continue
            # Drop stale source samples instead of accumulating a playback queue.
            while index+1<len(frames) and origin+int((frames[index+1].video_s-source_start)*1e9)<=now:
                index+=1;stats["dropped_frames"]+=1
            frame=frames[index];released=time.perf_counter_ns();t1=released
            if window:
                window.present(frame.full,frame.video_s,index);app.processEvents()
                acquired=window.capture()
                regions=acquired.regions;t1=acquired.capture_started_ns;t2=acquired.ready_ns;captured=acquired.captured_at
            else:
                regions=frame.regions;t2=time.perf_counter_ns();captured=time.monotonic()
            stats["capture_ms"].append((t2-t1)/1e6)
            t3=time.perf_counter_ns()
            found=pipeline.process(regions,replace(profile.offline_lane(),epoch=epoch),captured)
            stats["processing_ms"].append((time.perf_counter_ns()-t3)/1e6);stats["frames"]+=1
            if observation_sink is not None:
                # Numeric observations only, one explicitly selected trial per
                # clip/policy. Replays never multiply the accuracy denominator.
                observation_sink.append({"timestamp":frame.video_s,"observations":[o.to_dict() for o in found],
                    "raw_observations":[o.to_dict() for o in pipeline.raw_observations]})
            # Source and runtime clocks are mapped by actual releases, not subtracted directly.
            frame_stamps[captured]={"T1":t1,"T2":t2,"T3":t3,"release_ns":released,"video_s":frame.video_s}
            for raw in pipeline.raw_observations:
                key=(epoch,raw.champion,raw.source,pipeline.temporal.first_seen[raw.champion,raw.source])
                if key not in rows:
                    counter+=1;stamp=frame_stamps[key[-1]]
                    onset=SCENARIOS[scenario][3]
                    t0=None
                    if not window and raw.champion==SCENARIOS[scenario][2] and onset is not None:
                        t0=next((s["release_ns"] for s in frame_stamps.values() if abs(s["video_s"]-onset)<.00051),None)
                    rows[key]={"trial":trial,"warmup":warmup,"scenario":scenario,"policy":policy,"epoch":epoch,
                        "episode_id":f"{scenario}-{policy}-{trial}-{counter}","notification_id":"","eligibility":"synthetic_selected" if eligibility.synthetic else "verified_manifest" if eligibility.spec else "none_verified",
                        "source_video_s":stamp["video_s"],"source_onset_video_s":onset,"T0":t0,**{n:stamp[n] for n in ("T1","T2","T3")},
                        "T4":raw.detected_ns or time.perf_counter_ns(),"T5":None,"T6":None,"T7":None,"T8":None,"T9":None,"T10":None,"T11":None,
                        "champion":raw.champion,"source":raw.source,"position_x":raw.position[0],"position_y":raw.position[1],
                        "is_target":raw.champion==SCENARIOS[scenario][2],
                        "T0_method":"software sampled-frame release" if t0 else "unavailable","status":"raw_unconfirmed"}
            for item in found:
                key=(epoch,item.champion,item.source,item.first_seen_at)
                if key in rows and rows[key]["T5"] is None:
                    rows[key]["T5"]=item.confirmed_ns;rows[key]["status"]="not_eligible"
            eligible=() if suppress else eligibility.select(found,frame.video_s,scenario,profile)
            with lock:
                proposals=dispatcher.update(eligible,epoch)
                for ann in proposals:
                    item=dispatcher.offered[ann.event_key];key=(epoch,item.champion,item.source,item.first_seen_at)
                    row=rows[key]
                    if row["notification_id"]:
                        row={**row,**{s:None for s in ("T6","T7","T8","T9","T10","T11")}}
                        extra_rows.append(row)
                    row["T6"]=ann.queued_ns;row["notification_id"]=ann.event_key;row["status"]="proposed"
                    stats["capture_to_proposal_ms"].append((ann.queued_ns-row["T1"])/1e6)
                    notification_rows[ann.event_key]=row
                    if eligibility.synthetic:ann=replace(ann,text=f"Benchmark notification. {ann.champion} visible.")
                    voice_policy.offer(ann)
                ann=voice_policy.take(audio and active is None and interaction.allows_advice(time.monotonic()),defer_start=True)
            if ann:
                row=notification_rows[ann.event_key]
                start_speech(ann,row)
            if scenario=="new-match" and not injected and any(r.get("T7") for r in rows.values()):
                epoch+=1;pipeline.temporal.reset(epoch)
                with lock:dispatcher.update([],epoch);voice_policy.clear()
                injected=True;suppress=True;injection_ns=time.perf_counter_ns()
            dac=active[3].snapshot().get("device_playback") if active else None
            if scenario=="interrupt" and not injected and dac and time.perf_counter_ns()>=dac+100_000_000:
                active[2]["cancellation_reason"]="injected_user_interruption"
                interaction.begin_user_turn();active[0].stop();voice_policy.clear()
                injected=True;suppress=True;injection_ns=time.perf_counter_ns()
            index+=1
        # Visibility ends with the source; do not keep a historical target alive to finish speech.
        with lock:dispatcher.invalidate();voice_policy.clear()
        finish_speech(force=True);app.processEvents()
    finally:
        with lock:dispatcher.invalidate();voice_policy.clear()
        finish_speech(force=True)
    stats["processing_samples_ms"]=stats["processing_ms"]
    stats["processing"]=distribution(stats.pop("processing_ms"));stats["capture"]=distribution(stats.pop("capture_ms"))
    stats["capture_to_proposal"]=distribution(stats.pop("capture_to_proposal_ms"));stats["resources"]=resources.report()
    stats["injection_ns"]=injection_ns;stats["notifications"]=dict(dispatcher.counts)
    if not any(r.get("is_target") for r in rows.values()):
        extra_rows.append({"trial":trial,"warmup":warmup,"scenario":scenario,"policy":policy,"epoch":epoch,
            "episode_id":f"{scenario}-{policy}-{trial}-no-match","notification_id":"","eligibility":"none_verified",
            "is_target":True,"status":"no_raw_target_match",**{s:None for s in STAGES}})
    all_rows=list(rows.values())+extra_rows
    stats["expected"]=("No genuine alert without independently verified eligibility" if not eligibility.synthetic and not eligibility.spec
        else "Cancel obsolete notification before output" if scenario in ("disappear","new-match")
        else "Start test speech, then cancel on injected user interruption" if scenario=="interrupt"
        else "Suppress sighting speech while the real busy fixture speaks" if scenario=="already-speaking"
        else "At most one spoken notification per continuously visible champion")
    stats["target_output_count"]=sum(r.get("is_target",False) and r.get("T10") is not None for r in all_rows)
    stats["busy_fixture_output_count"]=sum(r.get("notification_id")=="benchmark:busy" and r.get("T10") is not None for r in all_rows)
    busy=[r for r in all_rows if r.get("notification_id")=="benchmark:busy" and r.get("T10")]
    stats["target_overlaps_busy"]=sum(r.get("is_target",False) and r.get("T10") is not None
        and any(b["T10"]<=r["T10"]<b.get("worker_finished_ns",r["T10"]+1) for b in busy) for r in all_rows)
    return all_rows,stats


def write_results(output,rows,trials,metadata):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(["trial","warmup","scenario","policy","epoch","episode_id","notification_id","eligibility","source_video_s","source_onset_video_s",*STAGES,"dac_ns","native_pcm_ns","status","loopback_error"]+[k for r in rows for k in r]))
    with (output/"trials.csv").open("w",newline="",encoding="utf-8") as stream:
        writer=csv.DictWriter(stream,fields);writer.writeheader();writer.writerows(rows)
    report={"metadata":metadata,"summary":summarize(rows),"trials":trials,
            "measurement":"Real detector and real speech path; software release/device/DAC/digital mix are separate, not acoustic output",
            "unique_recorded_examples_warning":"Repeated trials are latency samples, not independent accuracy examples"}
    save_json(output/"report.json",report)
    return report

"""Offline benchmark safety and correlation; no League, Ollama or audio devices."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from benchmarks.league_vision_e2e import (Eligibility,SCENARIOS,distribution,fingerprint,load_clip,summarize,write_results)
from core.latency import LatencyTrace,VoiceMetrics,current_trace
from core.league_visual import VisualCalibration,VisualObservation
from core.league_visual_evaluation import file_digest,save_json


@pytest.fixture
def setup(tmp_path):
    import cv2
    icon=np.random.default_rng(82).integers(20,240,(16,16,3),dtype=np.uint8)
    cv2.imwrite(str(tmp_path/"Warwick.png"),icon)
    cv2.imwrite(str(tmp_path/"Yuumi.png"),np.flip(icon,axis=0))
    spec={"reference_size":[128,128],"minimap":[0,0,1,1],"perspective":"player","fog_of_war":"player",
        "roster":[{"champion":"Warwick","side":"enemy"},{"champion":"Yuumi","side":"ally"}],
        "templates":{"Warwick":"Warwick.png","Yuumi":"Yuumi.png"},"fps":10,"threshold":.9,"margin":.08,
        "game_mode":"CLASSIC","map_number":11,"assigned_role":"UTILITY","player_champion":"Yuumi",
        "alerts_muted":False,"regions":[{"label":"bot","bounds":[0,0,1,1],"lane":"BOTTOM",
            "verification":{"kind":"operator_confirmed","frame_s":0}}]}
    calibration=tmp_path/"calibration.json";save_json(calibration,spec)
    video=tmp_path/"local.mp4";video.write_bytes(b"fingerprint-only fixture")
    return video,calibration,VisualCalibration(spec,tmp_path)


def observed(**changes):
    return replace(VisualObservation("Warwick","enemy","unknown",.98,True,100,"minimap",(.3,.3),(.2,.2,.1,.1),
        assigned_lane="BOTTOM",identity_margin=.4),**changes)


def manifest_for(video,calibration):
    return {"kind":"operator_confirmed_replay_eligibility","video":str(video),"video_fingerprint":fingerprint(video),
        "calibration_sha256":file_digest(calibration),"reviewer":{"kind":"operator_confirmed"},
        "selected_sightings":[{"champion":"Warwick","source":"minimap","start_s":1,"end_s":2,"position":[.3,.3]}]}


def test_assistant_review_needs_explicit_offline_opt_in_and_keeps_human_provenance_false(setup,tmp_path):
    video,calibration,profile=setup
    spec={**profile.spec,"regions":[{"label":"bot","bounds":[0,0,1,1],"lane":"BOTTOM",
          "verification":{"kind":"assistant_visual_audit","frame_s":1,"independent_human_review":False}}]}
    save_json(calibration,spec);profile=VisualCalibration(spec,tmp_path)
    value=manifest_for(video,calibration);value.update(kind="assistant_reviewed_replay_eligibility",
        reviewer={"kind":"assistant_visual_audit","independent_human_review":False})
    manifest=save_json(tmp_path/"assistant.json",value)
    with pytest.raises(ValueError):Eligibility(video,calibration,manifest)
    assert not profile.verified_regions  # production refuses assistant-only calibration
    gate=Eligibility(video,calibration,manifest,assistant_reviewed=True)
    gate.enable_reviewed_regions(profile)
    assert gate.assistant_reviewed and profile.verified_regions=={0}
    assert gate.select([observed(in_assigned_lane=True)],1.5,"warwick",profile)
    assert gate.spec["reviewer"]["independent_human_review"] is False
    assert not VisualCalibration(spec,tmp_path).verified_regions  # no global activation


@pytest.mark.parametrize("claim",[True,None,"false"])
def test_assistant_benchmark_cannot_claim_independent_human_review(setup,tmp_path,claim):
    video,calibration,_=setup
    value=manifest_for(video,calibration);value.update(kind="assistant_reviewed_replay_eligibility",
        reviewer={"kind":"assistant_visual_audit","independent_human_review":claim})
    manifest=save_json(tmp_path/"assistant.json",value)
    with pytest.raises(ValueError):Eligibility(video,calibration,manifest,assistant_reviewed=True)


@pytest.mark.parametrize("options",[["--mode","window"],["--eligibility","synthetic"]])
def test_assistant_review_cli_cannot_activate_captured_or_synthetic_modes(options):
    from benchmarks import benchmark_league_vision_e2e as cli
    with pytest.raises(SystemExit):cli.main(["run","--assistant-reviewed",*options])


def test_no_verified_eligibility_without_operator_manifest_even_if_detector_says_near(setup):
    video,calibration,profile=setup
    gate=Eligibility(video,calibration)
    assert not gate.select([observed(in_assigned_lane=True,near_player=True)],1.5,"warwick",profile)


def test_operator_manifest_is_bounded_current_and_scoped_to_exact_visual_evidence(setup,tmp_path):
    video,calibration,profile=setup;manifest=tmp_path/"eligibility.json"
    save_json(manifest,manifest_for(video,calibration));gate=Eligibility(video,calibration,manifest)
    o=observed(in_assigned_lane=True)
    assert gate.select([o],1.5,"warwick",profile)==(o,)
    for item,at in [(observed(),1.5),(replace(o,champion="Karma"),1.5),(replace(o,position=(.8,.8)),1.5),
                    (replace(o,source="viewport"),1.5),(o,3)]:
        assert not gate.select([item],at,"warwick",profile)
    video.write_bytes(b"changed")
    with pytest.raises(ValueError,match="unchanged"):Eligibility(video,calibration,manifest)


def test_operator_selected_clip_uses_recorded_interval_and_does_not_invent_onset(setup,tmp_path):
    from benchmarks.league_vision_e2e import verified_clip
    video,calibration,profile=setup;manifest=tmp_path/"eligibility.json"
    save_json(manifest,manifest_for(video,calibration));gate=Eligibility(video,calibration,manifest)
    assert verified_clip(gate)==(.5,2.,"Warwick",None)
    gate.spec["selected_sightings"][0].update(onset_known=True,onset_s=1.)
    assert verified_clip(gate)[3]==1.
    with pytest.raises(ValueError):verified_clip(gate,1)
    with pytest.raises(ValueError):verified_clip(Eligibility(video,calibration))


@pytest.mark.parametrize("change",[{"selected_sightings":[]},{"selected_sightings":[{}]},
    {"reviewer":{"kind":"assistant_reviewed"}},{"calibration_sha256":"changed"}])
def test_malformed_or_unreviewed_eligibility_fails_closed(setup,tmp_path,change):
    video,calibration,profile=setup;manifest=tmp_path/"eligibility.json"
    save_json(manifest,{**manifest_for(video,calibration),**change})
    with pytest.raises(ValueError):Eligibility(video,calibration,manifest)


def test_synthetic_eligibility_is_only_named_benchmark_target_and_never_invents_distance(setup):
    video,calibration,profile=setup;gate=Eligibility(video,calibration,synthetic=True)
    result=gate.select([observed(),observed(champion="Karma")],1,"synthetic",profile)
    assert len(result)==1 and result[0].champion=="Warwick"
    assert result[0].in_assigned_lane is True and result[0].distance is None
    assert result[0].near_player is None and result[0].player_position_source=="unverified"
    assert result[0].location=="offline test region"


def test_synthetic_source_exercises_real_template_detector_not_a_fake_delay(setup):
    from core.league_visual import LeagueVisualPipeline
    video,_,profile=setup;frames,metadata=load_clip(video,profile,"disappear")
    p=LeagueVisualPipeline(profile)
    assert "synthetic" in metadata["kind"]
    assert [f.video_s for f in frames[:5]]==[0,.1,.2,.3,.4]
    for f in frames:
        result=p.process(f.regions,profile.offline_lane(),f.video_s)
        if f.video_s==.6:assert result and result[0].champion=="Warwick"
        if f.video_s>=.7:assert not result


def test_summary_counts_paired_trials_once_retries_not_independent_accuracy_examples():
    row={"policy":"strict3","scenario":"synthetic","trial":0,"warmup":False,"is_target":True,
         "T0":0,"T1":1_000_000,"T5":200_000_000,"T8":202_000_000,"T9":600_000_000,
         "T10":650_000_000,"T11":680_000_000,"status":"started"}
    retry={**row,"T10":None,"T11":None,"status":"dropped"}
    report=summarize([row,retry,{**row,"trial":1,"T11":None},{**row,"trial":2,"warmup":True}])["strict3"]["synthetic"]
    assert report["paired_trials"]==2 and report["episode_rows"]==3
    assert report["timings"]["confirmation_to_loopback"]==distribution([480],1)
    assert report["timings"]["appearance_to_confirmation"]["count"]==2


def test_missing_synchronized_source_onset_remains_missing():
    row={"policy":"adaptive","scenario":"warwick","trial":0,"warmup":False,"is_target":True,
         "status":"not_eligible","T0":None,"T1":50,"T5":100}
    result=summarize([row])["adaptive"]["warwick"]["timings"]
    assert result["appearance_to_confirmation"]=={"count":0,"missing":1}
    assert result["capture_to_loopback"]=={"count":0,"missing":1}


@pytest.mark.parametrize("marks,onset,accepted",[({"T9":100,"T10":200},(220,230),True),
    ({"T9":100,"T10":200},(220,199),False),
    ({"T9":100,"T10":200},(199,230),False),({"T9":100},(220,230),False),
    ({"T10":200},(220,230),False),({"T9":100,"T10":200},None,False)])
def test_loopback_requires_pcm_callback_and_correlated_onset(marks,onset,accepted):
    from benchmarks.league_vision_e2e import attach_loopback
    row=dict(marks);attach_loopback(row,onset)
    assert (row.get("T11") is not None)==accepted
    if accepted:assert row["loopback_received_ns"]==230
    else:assert row.get("loopback_error")


def test_trace_snapshot_copy_first_mark_and_concurrent_access():
    trace=LatencyTrace(VoiceMetrics(True))
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda i:trace.mark("first",i+100),range(100)))
    first=trace.snapshot()["first"];snapshot=trace.snapshot();snapshot["first"]=-1
    assert trace.snapshot()["first"]==first
    assert distribution([])=={"count":0,"missing":0}
    assert distribution([1,2,3])["p95_ms"]==2


def test_warm_trial_readiness_reuses_worker_and_accounts_for_cancelled_native_reload():
    from benchmarks.league_vision_e2e import ensure_voice_ready
    session=SimpleNamespace(process=None);calls=[]
    def warm():
        calls.append(True)
        if session.process is None:session.process=SimpleNamespace(pid=23,poll=lambda:None)
    cold=ensure_voice_ready(warm,session);warm_result=ensure_voice_ready(warm,session)
    assert cold["reloaded"] and cold["before_pid"] is None and cold["after_pid"]==23
    assert warm_result["before_pid"]==warm_result["after_pid"]==23 and not warm_result["reloaded"]
    assert len(calls)==2 and cold["outside_timed_source"] and cold["check_ms"]>=0
    with pytest.raises(RuntimeError,match="persistent Kokoro"):
        ensure_voice_ready(lambda:None,SimpleNamespace(process=None))


def test_actual_advice_worker_uses_supplied_trace_and_existing_speech_path(monkeypatch):
    from core.context import Context
    from core.league_voice import LeagueAnnouncement
    from gui.workers import AdviceWorker
    context=Context();trace=LatencyTrace(VoiceMetrics(True));calls=[]
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED",True)
    monkeypatch.setattr(context.league,"announcement_current",lambda a:True)
    monkeypatch.setattr(context.league,"announcement_status",lambda a,s:None)
    def speak(text,**options):
        assert current_trace() is trace
        calls.append(text);trace.mark("audio_ready",123)
        options["before_playback"]();options["on_playback_start"]()
    monkeypatch.setattr("voice.tts.speak",speak)
    worker=AdviceWorker([],context,LeagueAnnouncement("Warwick visible.","visual:test",time.monotonic(),10),trace=trace)
    worker.run()
    assert len(calls)==1 and trace.snapshot()["audio_ready"]==123


def test_csv_only_numeric_timing_and_identifiers_no_private_pixels_or_speech(tmp_path):
    row={"trial":0,"policy":"strict3","scenario":"synthetic","warmup":False,"is_target":True,"status":"no_raw_target_match"}
    report=write_results(tmp_path,[row],[],{"live_gate":{"enabled":False,"approved":False}})
    assert "not independent" in report["unique_recorded_examples_warning"]
    assert sorted(p.name for p in tmp_path.iterdir())==["report.json","trials.csv"]
    assert "T11" in (tmp_path/"trials.csv").read_text()


def test_cli_cannot_silently_start_loopback_without_audio_or_unknown_scenario():
    from benchmarks import benchmark_league_vision_e2e as cli
    for argv in (["run","--loopback"],["run","--scenarios","live-game"],["run","--trials","0"]):
        with pytest.raises(SystemExit):cli.main(argv)


def test_detection_only_metadata_survives_absent_audio_hardware(monkeypatch):
    from benchmarks.benchmark_league_vision_e2e import output_metadata
    def unavailable(sd):raise ValueError("No output")
    monkeypatch.setattr("voice.playback._output_options",unavailable)
    assert "output_unavailable" in output_metadata(SimpleNamespace(PortAudioError=RuntimeError))


def test_model_contention_fails_closed_for_nonlocal_host(monkeypatch):
    from benchmarks.league_e2e_workload import ModelWorkload
    monkeypatch.setattr("config.OLLAMA_HOST","https://external.invalid")
    workload=ModelWorkload(True)
    assert not workload.threads and "loopback" in workload.error
    workload.close()


def test_replay_capture_refuses_a_game_or_foreign_window_before_capture(setup):
    import ctypes
    from benchmarks.league_replay_window import ReplayWindowCapture
    _,_,profile=setup
    window=SimpleNamespace(token="test-token",winId=lambda:12,isVisible=lambda:True)
    adapter=ReplayWindowCapture(profile,window)
    class ForeignAPI:
        def GetWindowThreadProcessId(self,handle,pid):ctypes.cast(pid,ctypes.POINTER(ctypes.c_ulong))[0]=99999
        def GetWindowTextW(self,handle,title,length):title.value="League of Legends"
    adapter.api=ForeignAPI()
    with pytest.raises(RuntimeError,match="own replay"):adapter.geometry()


@pytest.mark.parametrize("scenario,expected",[("synthetic",1),("continuous",1),("disappear",0),
    ("new-match",0),("interrupt",1),("already-speaking",0)])
def test_paced_source_real_notification_lifecycle_and_mocked_audio_safety(setup,qapp,monkeypatch,scenario,expected):
    """Real detector/policy/Qt worker, mock audio. Actual audio is a separate CLI test."""
    from threading import Event
    from core.cancellation import check_cancelled
    from benchmarks.league_vision_e2e import run_trial
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED",True)
    monkeypatch.setattr("config.LEAGUE_LATENCY_METRICS_ENABLED",True)
    def speak(text,**options):
        cancellation=options["cancel_event"];trace=current_trace();trace.mark("tts_submitted")
        if scenario!="already-speaking":cancellation.wait(.22)
        check_cancelled(cancellation);trace.mark("audio_ready")
        options["before_playback"]();trace.mark("device_submitted");trace.mark("device_playback")
        options["on_playback_start"]()
        cancellation.wait(1 if scenario in ("interrupt","already-speaking") else .02)
        if cancellation.is_set():trace.mark("output_abort_complete")
        check_cancelled(cancellation)
    monkeypatch.setattr("voice.tts.speak",speak)
    video,calibration,profile=setup
    frames,_=load_clip(video,profile,scenario)
    frames=frames[:8 if scenario=="already-speaking" else 13]
    gate=Eligibility(video,calibration,synthetic=True)
    rows,stats=run_trial(frames,profile,gate,scenario,"strict3",0,audio=True)
    target=[r for r in rows if r.get("is_target") and r.get("T10")]
    assert len(target)==expected
    assert all(r.get("T11") is None for r in rows)  # Never invent loopback from queued/mock audio.
    if scenario=="already-speaking":assert stats["busy_fixture_output_count"]==1
    if scenario=="interrupt":assert target[0]["abort_complete_ns"]>=target[0]["interruption_ns"]
    if scenario in ("new-match","disappear"):
        assert stats["notifications"]["accepted"]>=1 and not stats["notifications"]["started"]

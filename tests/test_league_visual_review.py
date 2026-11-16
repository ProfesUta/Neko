"""Offline evidence boundaries; no League, LLM or audio hardware required."""
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.league_visual import VisualCalibration, VisualObservation, VisualAlertDispatcher, ReviewedPlayerPosition
from core.league_visual_evaluation import save_json, file_digest
from core.league_visual_review import prepare_review, load_review, window_labels, finalize_review, score_review


def profile_spec():
    return {"reference_size":[128,128],"minimap":[0,0,1,1],"fps":10,
            "perspective":"player","fog_of_war":"player","game_mode":"CLASSIC","map_number":11,
            "assigned_role":"UTILITY","player_champion":"Yuumi","alerts_muted":False,
            "roster":[{"champion":"Warwick","side":"enemy"},{"champion":"Yuumi","side":"ally"}],
            "regions":[{"label":"bot lane","bounds":[0,0,.5,.5],"lane":"BOTTOM",
                        "verification":{"kind":"operator_confirmed","frame_s":1}}]}


def observation(**kwargs):
    return replace(VisualObservation("Warwick","enemy","bot lane",.98,True,100,"minimap",
                   (.2,.2),(.1,.1,.1,.1),assigned_lane="BOTTOM",in_assigned_lane=True),**kwargs)


@pytest.fixture
def review(tmp_path,monkeypatch):
    video=tmp_path/"player.mp4";video.write_bytes(b"synthetic video fingerprint")
    reference=tmp_path/"Warwick.png";reference.write_bytes(b"synthetic reference")
    spec=profile_spec();spec["templates"]={"Warwick":"Warwick.png"}
    calibration=save_json(tmp_path/"calibration.json",spec)
    import cv2
    monkeypatch.setattr("core.league_visual_review.open_recording",lambda p:SimpleNamespace(
        get=lambda key:10 if key==cv2.CAP_PROP_FPS else 1000,isOpened=lambda:True,release=lambda:None))
    output=tmp_path/"review"
    plan=prepare_review(video,calibration,output,windows=2,seconds=2)
    return output/"plan.json",plan


def test_blind_plan_has_disjoint_splits_bound_to_all_reference_assets(review):
    path,plan=review
    assert load_review(path)==plan
    assert [w["split"] for w in plan["windows"]]==["tuning","tuning","heldout","heldout"]
    windows=sorted((w["samples_s"][0],w["samples_s"][-1]) for w in plan["windows"])
    assert all(a[1]<b[0] for a,b in zip(windows,windows[1:]))
    Path(next(iter(plan["reference_sha256"]))).write_bytes(b"changed reference")
    with pytest.raises(ValueError,match="changed"):load_review(path)


@pytest.mark.parametrize("change",["calibration","video"])
def test_review_rejects_changed_inputs(review,change):
    path,plan=review
    Path(plan[change]).write_bytes(b"changed")
    with pytest.raises((ValueError,KeyError)):load_review(path)


def test_review_rejects_tuning_heldout_overlap_and_escaping_label_paths(review):
    path,plan=review
    bad=copy.deepcopy(plan);bad["windows"][2]["samples_s"]=bad["windows"][0]["samples_s"]
    save_json(path,bad)
    with pytest.raises(ValueError,match="overlap"):load_review(path)
    plan["windows"][0]["labels"]="../outside.json";save_json(path,plan)
    with pytest.raises(ValueError,match="inside"):load_review(path)


def test_review_requires_all_negative_frames_and_explicit_human_confirmation(review):
    path,plan=review;window=plan["windows"][0]
    _,_,value=window_labels(path,window["id"])
    assert not value["labels"] and value["reviewer"]["independent_human_review"] is False
    with pytest.raises(ValueError,match="every planned frame"):finalize_review(value,path,window["id"],human_confirmed=True)
    value["reviewed_samples_s"]=window["samples_s"]
    with pytest.raises(ValueError,match="explicitly confirm"):finalize_review(value,path,window["id"])
    result=finalize_review(value,path,window["id"],human_confirmed=True)
    assert result["annotation_complete"] and not result["labels"]  # genuine negative coverage
    assert not value["annotation_complete"]  # no mutation


def test_candidates_cannot_become_blind_ground_truth(review):
    path,plan=review;window=plan["windows"][0];_,_,value=window_labels(path,window["id"])
    value["reviewed_samples_s"]=window["samples_s"]
    value["reviews"]=[{"candidate_id":"candidate-1","decision":"confirmed"}]
    with pytest.raises(ValueError,match="not blind"):finalize_review(value,path,window["id"],human_confirmed=True)


def test_unreviewed_or_assistant_windows_do_not_report_independent_accuracy(review):
    path,plan=review
    report=save_json(path.parent/"detections.json",{k:plan[k] for k in ("calibration_sha256","video_fingerprint","reference_sha256")})
    result=score_review(path,report)
    assert not result["complete"] and result["independently_reviewed_sightings"]==0
    window=plan["windows"][2];_,_,labels=window_labels(path,window["id"])
    labels["reviewer"]={"type":"assistant_visual_audit","independent_human_review":False}
    save_json(path.parent/window["labels"],labels)
    assert not score_review(path,report)["complete"]


def test_independent_split_scores_misses_negatives_and_wrong_id_only_after_completion(review):
    import json
    path,plan=review;rows=[]
    for index,window in enumerate(w for w in plan["windows"] if w["split"]=="heldout"):
        _,_,labels=window_labels(path,window["id"])
        labels["reviewed_samples_s"]=window["samples_s"]
        labels["ignored_regions"]=[{"source":"minimap","start_s":at,"end_s":at,"bounds":[0,0,1,1]}
                                   for i,at in enumerate(window["samples_s"]) if index!=0 or i>=3]
        if index==0:
            for at in window["samples_s"][:2]:
                labels["labels"].append({"champion":"Warwick","source":"minimap","start_s":at,"end_s":at,
                                        "position":[.2,.2],"episode_id":"visible-1","onset_known":False})
        labels=finalize_review(labels,path,window["id"],human_confirmed=True)
        save_json(path.parent/window["labels"],labels)
        for i,at in enumerate(window["samples_s"]):
            obs=[]
            if index==0 and i<3:
                obs=[observation(champion="Darius" if i in (0,2) else "Warwick",timestamp=at).to_dict()]
            rows.append({"timestamp":at,"observations":obs})
    (path.parent/"frames.jsonl").write_text("\n".join(json.dumps(r) for r in rows),encoding="utf-8")
    report={**{k:plan[k] for k in ("video","calibration","calibration_sha256","video_fingerprint","reference_sha256")},
            "candidates":[],"frames":"frames.jsonl","metrics":{},"cpu_seconds":0,"wall_seconds":1,"analyzed_fps":10}
    score=score_review(path,save_json(path.parent/"detections.json",report))
    assert score["complete"] and score["independently_reviewed_sightings"]==1
    assert score["accuracy"]["true_positive_frames"]==1
    assert score["accuracy"]["missed_positive_frames"]==1
    assert score["accuracy"]["false_positive_frames"]==2
    assert score["accuracy"]["incorrect_identity_frames"]==1
    assert score["accuracy"]["reviewed_source_seconds"]==pytest.approx(.3)
    assert score["accuracy"]["false_positive_episodes_per_minute"]==pytest.approx(200.)


@pytest.mark.parametrize("verified",[False,True])
def test_lane_geometry_and_operator_evidence_are_both_required(tmp_path,verified):
    spec=profile_spec()
    if not verified:spec["regions"][0].pop("verification")
    p=VisualCalibration(spec,tmp_path);d=VisualAlertDispatcher(p)
    assert d.eligible(observation()) is verified
    assert not d.eligible(observation(position=(.8,.8)))  # asserted flag cannot invent a region
    assert not d.eligible(observation(in_assigned_lane=False))


def test_other_lane_and_unknown_player_are_distinct_and_neither_implies_proximity(tmp_path):
    spec=profile_spec();spec["regions"][0]["lane"]="TOP"
    profile=VisualCalibration(spec,tmp_path)
    assert profile.relevance(observation())==("verified other lane region",False)
    assert profile.relevance(observation(position=(.8,.8)))==("unknown player position",False)
    assert profile.relevance(observation(position=(.8,.8),player_position_source="verified own minimap icon"))==("insufficient location evidence",False)


@pytest.mark.parametrize("changes",[{"independent":False},{"attached":True},{"epoch":2},
                                   {"timestamp":99.9},{"position":(float("nan"),.5)}])
def test_reviewed_player_points_never_interpolate_or_infer_host_position(changes):
    point=ReviewedPlayerPosition((.2,.2),100,1,True,False)
    assert point.usable(100,1)
    assert not replace(point,**changes).usable(100,1)


def test_replay_player_evidence_is_exact_frame_bounded_and_cannot_be_an_attached_host(tmp_path):
    from benchmarks.league_vision_e2e import Eligibility, fingerprint
    video=tmp_path/"video.mp4";video.write_bytes(b"test")
    calibration=save_json(tmp_path/"calibration.json",profile_spec())
    value={"kind":"operator_confirmed_replay_eligibility","video":str(video),"video_fingerprint":fingerprint(video),
           "calibration_sha256":file_digest(calibration),"reviewer":{"kind":"operator_confirmed"},
           "selected_sightings":[{"champion":"Warwick","source":"minimap","position":[.2,.2],"start_s":1,"end_s":2}],
           "player_locations":[{"video_s":1.,"position":[.2,.25],"independent":True,"attached":False}]}
    manifest=save_json(tmp_path/"eligible.json",value)
    gate=Eligibility(video,calibration,manifest)
    assert gate.player_at(1.,100.,3).usable(100.,3)
    assert gate.player_at(1.1,100.1,3) is None
    value["player_locations"][0]["attached"]=True;save_json(manifest,value)
    with pytest.raises(ValueError,match="host inference"):Eligibility(video,calibration,manifest)


def test_verified_region_does_not_claim_yuumi_distance_and_live_gate_stays_closed(tmp_path):
    profile=VisualCalibration(profile_spec(),tmp_path);d=VisualAlertDispatcher(profile)
    item=observation()
    assert item.near_player is None and item.distance is None
    assert profile.relevance(item)==("verified assigned lane region",True)
    assert not d.update([item],1,now=100,live=True,approved=False)
    assert d.update([item],1,now=100,live=False)[0].priority==0


def test_own_icon_proximity_must_be_within_calibrated_radius(tmp_path):
    profile=VisualCalibration(profile_spec(),tmp_path)
    item=observation(position=(.8,.8),near_player=True,distance=.01,
                     player_position_source="independently reviewed recording frame")
    assert profile.relevance(item)==("verified player proximity",True)
    assert not profile.relevance(replace(item,distance=.4))[1]


def test_benchmark_stage_pairs_report_missing_output_without_faking_latency():
    from benchmarks.league_vision_e2e import summarize
    rows=[{"trial":1,"policy":"strict3","scenario":"verified","is_target":True,"status":"not_eligible",
           "T0":1_000_000,"T4":2_000_000,"T5":202_000_000}]
    timings=summarize(rows)["strict3"]["verified"]["timings"]
    assert timings["raw_to_confirmation"]["p50_ms"]==200
    assert timings["pcm_ready_to_loopback"]["count"]==0


def test_blind_gui_hides_candidates_marks_absence_and_uses_existing_frame_worker(review,qapp,monkeypatch):
    from gui.league_calibration import LeagueCalibrationDialog
    path,plan=review;window=plan["windows"][0]
    # UI wiring test; actual decoder lifecycle is tested in test_league_visual.
    monkeypatch.setattr(LeagueCalibrationDialog,"load_frame",lambda self:None)
    d=LeagueCalibrationDialog(plan["calibration"],plan["video"],labels=str(path.parent/window["labels"]))
    try:
        d.begin_blind_review(path,window["id"])
        assert not d.tabs.isTabEnabled(0) and not d.tabs.isTabEnabled(1)
        assert d.tabs.currentIndex()==2 and not d.human_review.isChecked()
        with pytest.raises(ValueError,match="hidden"):d.load_detections(path)
        d.loaded_at=window["samples_s"][0];d.loaded_video=plan["video"]
        d.mark_review_frame()
        assert d.manifest["reviewed_samples_s"]==[window["samples_s"][0]]
        assert not d.manifest["labels"] and not d.manifest["annotation_complete"]
        assert d.worker is None
    finally:d.close();d.deleteLater()


def test_actual_review_frame_does_not_expand_frozen_coverage_to_entire_video(tmp_path,qapp,wait_until):
    import cv2
    import numpy as np
    from gui.league_calibration import LeagueCalibrationDialog
    video=tmp_path/"synthetic.avi";writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*"MJPG"),10,(128,128))
    assert writer.isOpened()
    for i in range(200):writer.write(np.zeros((128,128,3),np.uint8))
    writer.release()
    spec=profile_spec();spec["minimap"]=[.5,.5,.5,.5]
    calibration=save_json(tmp_path/"calibration.json",spec)
    plan=prepare_review(video,calibration,tmp_path/"review",windows=1,seconds=2)
    path=tmp_path/"review"/"plan.json";window=plan["windows"][0]
    d=LeagueCalibrationDialog(calibration,str(video),labels=str(path.parent/window["labels"]))
    try:
        d.begin_blind_review(path,window["id"])
        wait_until(lambda:d.worker is None and d.loaded_at is not None)
        assert d.review_frame_loaded()
        assert d.cover_end.value()==window["samples_s"][-1]
        assert d.canvas.image.width()==64  # enlarged cached ROI, not a second decode
        d.point_selected((.25,.5));assert d.x.value()==.25 and d.y.value()==.5
        assert d.editor.isReadOnly()
        for at in window["samples_s"]:d.manifest.setdefault("reviewed_samples_s",[]).append(at)
        d.human_review.setChecked(True);d.finish_review()
        import json
        saved=json.loads(d.label_path.read_text())
        assert saved["annotation_complete"]
        assert saved["coverage"]["end_s"]==window["samples_s"][-1]
    finally:d.close();d.deleteLater()

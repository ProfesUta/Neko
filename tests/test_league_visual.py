import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import numpy as np
import pytest

cv2=pytest.importorskip("cv2")

import config
from core.league_lane import LaneTracker,LaneContext
from core.league_visual import (VisualCalibration,LeagueVisualPipeline,VisualObservation,
                               TemporalConfirmation,VisualAlertDispatcher,ViewportMatcher)
from core.league_capture import RegionFrame,physical_regions,GameWindowCapture
from core.live_screen import FrameHistory
from core.league_visual_evaluation import analyze_recording,new_labels,save_json,evaluate_labels,validate_labels


@pytest.fixture
def visual_fixture(tmp_path):
    rng=np.random.default_rng(82)
    images={name:rng.integers(20,240,(16,16,3),dtype=np.uint8) for name in ("Lee Sin","Ahri","Garen")}
    for name,image in images.items():cv2.imwrite(str(tmp_path/f"{name}.png"),image)
    spec={"reference_size":[128,128],"minimap":[0,0,1,1],"perspective":"player","fog_of_war":"player",
          "roster":[{"champion":"Lee Sin","side":"enemy","position":"JUNGLE"},
                    {"champion":"Garen","side":"enemy","position":"TOP"},
                    {"champion":"Ahri","side":"ally","position":"MIDDLE"}],
          "templates":{name:f"{name}.png" for name in images},"threshold":.9,"margin":.08,
          "fps":10,"game_mode":"CLASSIC","map_number":11,"assigned_role":"MIDDLE","player_champion":"Ahri",
          "alerts_muted":False,"regions":[{"label":"mid lane region","bounds":[0,0,.5,.5],"lane":"MIDDLE"}]}
    def frame(target=True):
        output=np.zeros((128,128,3),np.uint8);output[90:106,90:106]=images["Ahri"]
        if target:output[20:36,20:36]=images["Lee Sin"]
        return output
    path=tmp_path/"calibration.json";save_json(path,spec)
    return spec,images,frame,tmp_path


@pytest.mark.parametrize("role,lane",[("TOP","TOP"),("JUNGLE",None),("MIDDLE","MIDDLE"),("BOTTOM","BOTTOM"),("UTILITY","BOTTOM")])
def test_assigned_role_survives_loading_transport_gaps_and_match(role,lane):
    changes=[];tracker=LaneTracker(on_change=changes.append)
    tracker.phase("ChampSelect",role,True)
    tracker.phase("Unavailable");tracker.phase("GameStart");tracker.phase("InProgress")
    tracker.update(2,"CLASSIC",11,"","ORDER","Yuumi")
    state=tracker.snapshot()
    assert state.role==role and state.assigned_lane==lane and state.supported
    tracker.update(2,"CLASSIC",11,"","ORDER","Yuumi")
    assert tracker.snapshot()==state and changes[-1]==state
    tracker.phase("Reconnect");assert tracker.snapshot()==state


def test_reconnect_fallback_unsupported_modes_and_new_matches_do_not_guess_champion_role():
    tracker=LaneTracker("UTILITY");tracker.update(1,"ARAM",12,"","ORDER","Yuumi")
    assert tracker.snapshot().role=="UTILITY" and not tracker.snapshot().supported
    tracker.update(1,"CLASSIC",11,"JUNGLE","ORDER","Ahri")
    assert tracker.snapshot().role=="JUNGLE"
    tracker.reset(1);tracker.update(2,"CLASSIC",11,"","ORDER","Ahri")
    assert tracker.snapshot().role=="UTILITY" and "fallback" in tracker.snapshot().role_source
    unknown=LaneTracker();unknown.update(1,"CLASSIC",11,"","ORDER","Yuumi")
    assert unknown.snapshot().role is None and unknown.snapshot().assigned_lane is None
    unknown.phase("ChampSelect","TOP",True);unknown.update(2,"CLASSIC",11)
    unknown.phase("Lobby");unknown.phase("ChampSelect","BOTTOM",True);unknown.update(3,"CLASSIC",11)
    assert unknown.snapshot().assigned_lane=="BOTTOM"


def test_concurrent_lane_snapshots_are_immutable_and_copy_safe():
    tracker=LaneTracker()
    def operation(index):
        tracker.update(1,"CLASSIC",11,"MIDDLE","ORDER","Ahri")
        result=tracker.snapshot();result.to_dict()["role"]="TOP";return result
    with ThreadPoolExecutor(4) as pool:states=list(pool.map(operation,range(40)))
    assert all(s.role=="MIDDLE" for s in states)
    with pytest.raises(Exception):states[0].role="TOP"


@pytest.mark.parametrize("change",[{"hud_exclusions":None},{"blockers":None},{"viewport_nameplates":[]},
    {"team_orientation":"guess"},{"enemy_bar_color":"blue"},{"proximity_radius":float("nan")},{"alerts_muted":"false"},
    {"minimap":[-.1,0,.2,.2]},{"fallback_role":"ADC"},{"fps":100},{"reference_size":[1920,0]}])
def test_bad_calibrations_are_rejected(visual_fixture,change):
    spec,_,_,root=visual_fixture
    with pytest.raises(ValueError):VisualCalibration({**spec,**change},root)


def confirm_frames(pipeline,profile,frame,start=0):
    return [pipeline.process(profile.regions_from_frame(frame),profile.offline_lane(),start+i*.1) for i in range(3)]


def test_visible_enemy_vs_allies_temporal_confirmation_and_no_fog_extrapolation(visual_fixture):
    spec,images,frame,root=visual_fixture
    profile=VisualCalibration(spec,root);pipeline=LeagueVisualPipeline(profile)
    results=confirm_frames(pipeline,profile,frame())
    assert results[:2]==[(),()] and len(results[2])==1
    found=results[2][0]
    assert found.champion=="Lee Sin" and found.team=="enemy" and found.in_assigned_lane
    assert found.near_player is False and found.first_seen_at==0
    assert not any(item.champion=="Ahri" for item in results[2])
    assert pipeline.process(profile.regions_from_frame(frame(False)),profile.offline_lane(),.3)==()
    assert pipeline.process(profile.regions_from_frame(frame()),profile.offline_lane(),.4)==()
    hidden=frame(False);hidden[20:36,20:36]=images["Ahri"]
    assert all(not r for r in confirm_frames(pipeline,profile,hidden,1))


def test_own_position_not_invented_and_proximity_uses_only_verified_player_icon(visual_fixture):
    spec,images,frame,root=visual_fixture
    near=frame();near[90:106,90:106]=0;near[20:36,42:58]=images["Ahri"]
    profile=VisualCalibration({**spec,"proximity_radius":.3},root)
    observation=confirm_frames(LeagueVisualPipeline(profile),profile,near)[-1][0]
    assert observation.near_player is True
    near[20:36,42:58]=0
    observation=confirm_frames(LeagueVisualPipeline(profile),profile,near)[-1][0]
    assert observation.near_player is None and observation.distance is None
    profile=VisualCalibration({**spec,"player_champion":"Lee Sin"},root)
    assert confirm_frames(LeagueVisualPipeline(profile),profile,frame())[-1][0].near_player is None


def test_attached_yuumi_host_face_and_ambiguous_allies_do_not_establish_player_position(visual_fixture):
    spec,images,frame,root=visual_fixture
    spec=copy.deepcopy(spec)
    spec["roster"].append({"champion":"Yuumi","side":"ally","position":"UTILITY"})
    spec["templates"]["Yuumi"]="Ahri.png"  # An attached marker/reference showing a host's face.
    profile=VisualCalibration({**spec,"player_champion":"Yuumi"},root)
    pipeline=LeagueVisualPipeline(profile);match=pipeline.minimap.match;asked=[]
    def tracked(crop,name,gray=None):asked.append(name);return match(crop,name,gray)
    pipeline.minimap.match=tracked
    found=confirm_frames(pipeline,profile,frame())[-1][0]
    assert "Yuumi" not in asked and found.distance is None and found.near_player is None
    assert found.player_position_source=="unverified"
    # The same portrait belonging to two known allies is ambiguous even for a
    # non-attached player; the full roster must win the identity comparison.
    profile=VisualCalibration({**spec,"player_champion":"Ahri"},root)
    found=confirm_frames(LeagueVisualPipeline(profile),profile,frame())[-1][0]
    assert found.near_player is None and found.distance is None


@pytest.mark.parametrize("position",[(float("nan"),.2),(-.1,.2),(.2,1.1)])
def test_invalid_player_coordinates_never_produce_proximity_claims(visual_fixture,position):
    spec,_,frame,root=visual_fixture;profile=VisualCalibration(spec,root);pipeline=LeagueVisualPipeline(profile)
    match=pipeline.minimap.match
    pipeline.minimap.match=lambda crop,name,gray=None:({"minimap_position":position} if name=="Ahri" else match(crop,name,gray))
    found=confirm_frames(pipeline,profile,frame())[-1][0]
    assert found.near_player is None and found.distance is None


def test_two_claimed_identities_in_one_overlapping_icon_are_rejected(visual_fixture):
    spec,_,frame,root=visual_fixture;profile=VisualCalibration(spec,root);pipeline=LeagueVisualPipeline(profile)
    pipeline.minimap.match=lambda crop,name,gray=None:None if name=="Ahri" else {
        "minimap_position":(.2,.2),"pixel_box":(20,20,16,16),"template_similarity":.99,"ambiguity_margin":.4}
    assert not any(confirm_frames(pipeline,profile,frame()))


def test_different_resolutions_scaling_low_confidence_and_obscured_icons(visual_fixture):
    spec,images,frame,root=visual_fixture
    profile=VisualCalibration(spec,root)
    large=cv2.resize(frame(),(256,256),interpolation=cv2.INTER_NEAREST)
    assert confirm_frames(LeagueVisualPipeline(profile),profile,large)[-1][0].position==pytest.approx((28/128,28/128))
    hidden=frame();hidden[20:31,20:36]=0
    assert not any(confirm_frames(LeagueVisualPipeline(profile),profile,hidden))
    with pytest.raises(ValueError):profile.regions_from_frame(np.zeros((128,256,3),np.uint8))


def test_fractional_roi_edges_do_not_stretch_saved_minimap_icon_references(visual_fixture):
    spec,_,frame,root=visual_fixture
    profile=VisualCalibration({**spec,"minimap":[.1,.1,.7,.6]},root)
    pipeline=LeagueVisualPipeline(profile)
    image=frame()
    assert (pipeline.minimap.width,pipeline.minimap.height)==(89,77)
    np.testing.assert_array_equal(pipeline.minimap.crop(image),image[13:90,13:102])
    assert profile.regions_from_frame(image)["minimap"].shape[:2]==(77,89)
    result=confirm_frames(pipeline,profile,image)[-1]
    assert len(result)==1 and result[0].champion=="Lee Sin" and result[0].confidence>.99


def test_temporal_confirmation_resets_on_epoch_movement_or_time_regression():
    item=VisualObservation("Lee Sin","enemy","mid",.99,True,0,"minimap",(.2,.2),(.1,.1,.1,.1))
    tracker=TemporalConfirmation()
    for t in (0,.1,.2):result=tracker.update([replace(item,timestamp=t)],t,1)
    assert result
    assert not tracker.update([replace(item,timestamp=.3)],.3,2)
    assert not tracker.update([replace(item,timestamp=.4,position=(.8,.8))],.4,2)
    assert not tracker.update([replace(item,timestamp=.1)],.1,2)


@pytest.mark.parametrize("sigma",[-.1,1.3,float("nan"),True,".8"])
def test_invalid_minimap_smoothing_is_rejected(visual_fixture,sigma):
    spec,_,_,root=visual_fixture
    with pytest.raises(ValueError):LeagueVisualPipeline(VisualCalibration({**spec,"minimap_blur_sigma":sigma},root))


def test_calibrated_smoothing_is_shared_once_without_modifying_pixels_or_assets(visual_fixture,monkeypatch):
    spec,images,frame,root=visual_fixture
    profile=VisualCalibration({**spec,"minimap_blur_sigma":.8,"threshold":.85},root)
    pipeline=LeagueVisualPipeline(profile)
    image=frame();original=image.copy();files={name:(root/f"{name}.png").read_bytes() for name in images}
    calls=[];prepare=pipeline.minimap.prepare
    monkeypatch.setattr(pipeline.minimap,"prepare",lambda crop:(calls.append(crop.shape),prepare(crop))[1])
    result=confirm_frames(pipeline,profile,image)[-1]
    assert len(calls)==3 and result and result[0].champion=="Lee Sin"
    np.testing.assert_array_equal(original,image)
    assert files=={name:(root/f"{name}.png").read_bytes() for name in images}
    absent=frame(False)
    assert pipeline.process(profile.regions_from_frame(absent),profile.offline_lane(),.4)==()


def test_scoreboard_blocker_prevents_hud_portraits_becoming_sightings(visual_fixture):
    spec,images,frame,root=visual_fixture
    blocker=np.random.default_rng(90).integers(0,255,(8,20,3),dtype=np.uint8)
    cv2.imwrite(str(root/"scoreboard.png"),blocker)
    spec={**spec,"blockers":[{"image":"scoreboard.png","region":[0,.5,1,.3]}]}
    profile=VisualCalibration(spec,root);pipeline=LeagueVisualPipeline(profile)
    image=frame();image[70:78,50:70]=blocker
    assert not any(confirm_frames(pipeline,profile,image))


def test_viewport_needs_calibrated_nameplates_enemy_bars_and_excludes_hud(visual_fixture):
    spec,images,frame,root=visual_fixture
    spec={**spec,"reference_size":[256,128],"minimap":[.75,.5,.25,.5],"viewport":[0,0,.75,1],
          "blockers":[{"region":[0,0,.3,.1],"image":"unused.png"}],"viewport_nameplates":{}}
    names={}
    rng=np.random.default_rng(345)
    for name in images:
        image=rng.integers(50,240,(10,24),dtype=np.uint8);names[name]=image
        cv2.imwrite(str(root/f"plate-{name}.png"),image)
        spec["viewport_nameplates"][name]=[f"plate-{name}.png"]
    profile=VisualCalibration(spec,root);matcher=ViewportMatcher(profile);lane=profile.offline_lane()
    image=np.zeros((128,192,3),np.uint8);image[30:40,50:74]=names["Lee Sin"][:,:,None]
    assert not matcher.detect(image,["Lee Sin"],0,lane) # Portrait/text alone cannot prove an enemy unit.
    image[42:45,40:100]=(0,0,255)
    assert matcher.detect(image,["Lee Sin"],0,lane)[0].source=="viewport"
    image[42:45,40:100]=(255,0,0)
    assert not matcher.detect(image,["Lee Sin"],0,lane)
    image[42:45,40:100]=(0,0,255)
    profile=VisualCalibration({**spec,"hud_exclusions":[[0,0,.5,.5]]},root)
    assert not ViewportMatcher(profile).detect(image,["Lee Sin"],0,lane)
    incomplete=copy.deepcopy(spec);incomplete["viewport_nameplates"].pop("Ahri")
    assert not VisualCalibration(incomplete,root).viewport_enabled


def test_notifications_are_optional_fresh_deduplicated_and_lower_priority(visual_fixture):
    from core.league_voice import LeagueVoicePolicy,LeagueAnnouncement
    spec,_,_,root=visual_fixture
    profile=VisualCalibration(spec,root);dispatcher=VisualAlertDispatcher(profile)
    item=VisualObservation("Lee Sin","enemy","mid lane region",.99,True,10,"minimap",(.2,.2),(.1,.1,.1,.1),in_assigned_lane=True)
    assert dispatcher.update([item],1,now=10,live=True,approved=False)==()
    proposal=dispatcher.update([item],1,now=10,live=True,approved=True)[0]
    assert proposal.priority==0 and dispatcher.is_current(proposal,10)
    assert not dispatcher.update([replace(item,timestamp=10.1)],1,now=10.1)
    assert not dispatcher.is_current(proposal,10.6)
    dispatcher.update([],1,now=10.2);assert not dispatcher.is_current(proposal,10.2)
    dispatcher.update([replace(item,timestamp=10.3,in_assigned_lane=False)],1,now=10.3)
    assert not dispatcher.is_current(proposal,10.3)
    dispatcher.muted=True;assert not dispatcher.update([replace(item,timestamp=20)],1,now=20)
    dispatcher.muted=False;new=dispatcher.update([replace(item,timestamp=20)],2,now=20)[0]
    assert not dispatcher.is_current(proposal,20) and dispatcher.is_current(new,20)
    policy=LeagueVoicePolicy()
    user_event=LeagueAnnouncement("important","test",time.monotonic(),10,3)
    policy.offer(user_event)
    policy.offer(replace(new,captured_at=time.monotonic()))
    assert policy.pending==user_event


def test_raw_frame_history_is_bounded_stale_and_geometry_safe():
    history=FrameHistory(max_frames=2,max_bytes=1200)
    for i in range(5):history.put(RegionFrame({"minimap":np.zeros((10,10,3),np.uint8)},i*.1,i,"game",((0,0,100,100),)))
    assert history.status()["frames"]==2
    assert history.sample(1,max_age=.2,now=1).frames==()
    before=history.sample(1,max_age=.1,now=.4).epoch
    history.put(RegionFrame({"minimap":np.zeros((10,10,3),np.uint8)},.5,6,"game",((-1920,0,100,100),)))
    assert history.sample(1,max_age=.1,now=.5).epoch>before
    assert physical_regions((-1920,-300,2560,1440),{"minimap":(.8,.7,.2,.3)})["minimap"]=={"left":128,"top":708,"width":512,"height":432}
    with pytest.raises(ValueError):history.put(RegionFrame({"minimap":np.zeros((30,30,3),np.uint8)},.6,7,"game",()))


def test_disabled_capture_does_not_discover_windows_or_read_desktop(visual_fixture):
    spec,_,_,root=visual_fixture
    capture=GameWindowCapture(VisualCalibration(spec,root),lambda:False)
    class Forbidden:
        def windows(self):raise AssertionError("No scan while gate is off")
    capture.grounder=Forbidden()
    with pytest.raises(RuntimeError,match="not approved"):capture.geometry()


def test_game_client_geometry_uses_physical_negative_coordinates_and_rejects_occlusion(visual_fixture):
    from types import SimpleNamespace
    spec,_,_,root=visual_fixture
    profile=VisualCalibration({**spec,"reference_size":[1920,1080],"minimap":[.8,.7,.2,.3]},root)
    class API:
        foreground=7
        obscured=False
        def GetForegroundWindow(self):return self.foreground
        def IsWindow(self,handle):return True
        def IsWindowVisible(self,handle):return True
        def IsIconic(self,handle):return False
        def GetClientRect(self,handle,p):p._obj.right=1920;p._obj.bottom=1080;return True
        def ClientToScreen(self,handle,p):p._obj.x=-2560;p._obj.y=-200;return True
        def GetWindow(self,handle,which):return 8 if handle==7 and self.obscured else 0
        def GetWindowRect(self,handle,p):
            p._obj.left=-1000;p._obj.top=600;p._obj.right=-640;p._obj.bottom=880;return True
    grounder=SimpleNamespace(windows=lambda:[SimpleNamespace(handle=7,application="League of Legends.exe")])
    capture=GameWindowCapture(profile,lambda:True,grounder);capture.api=API()
    regions,client=capture.geometry()
    assert client==(-2560,-200,1920,1080)
    assert regions["minimap"]=={"left":-1024,"top":556,"width":384,"height":324}
    capture.api.obscured=True
    with pytest.raises(RuntimeError,match="obscured"):capture.geometry()
    capture.api.foreground=9
    with pytest.raises(RuntimeError,match="foreground"):capture.geometry()


def test_live_worker_recovers_from_temporary_data_outage_without_another_polling_worker(visual_fixture,qapp,wait_until,monkeypatch):
    from types import SimpleNamespace
    from gui.league_vision import LeagueVisualWorker
    spec,_,frame,root=visual_fixture
    monkeypatch.setattr(config,"LEAGUE_VISION_ENABLED",True);monkeypatch.setattr(config,"LEAGUE_VISION_RIOT_APPROVED",True)
    monkeypatch.setattr("gui.league_vision.inference_priority.background_ready",lambda:True)
    class League:
        session_active=True
        context=VisualCalibration(spec,root).offline_lane()
        on_notification=None
        def visual_context(self):return self.context
        def roster(self):return spec["roster"]
    class Stream:
        def __init__(self,factory,history,*args,**kwargs):self.history=history
        def start(self):self.put(1)
        def put(self,sequence):
            self.history.put(RegionFrame({"minimap":frame()},time.monotonic(),sequence,"game",()))
        def stop(self):self.history.clear()
        def join(self):pass
        def status(self):return {}
    monkeypatch.setattr("gui.league_vision.LiveScreenStream",Stream)
    monkeypatch.setattr("core.league_capture.GameWindowCapture",lambda *args:SimpleNamespace(open=lambda:None))
    league=League();worker=LeagueVisualWorker(league,root/"calibration.json")
    worker.start()
    try:
        wait_until(lambda:worker.snapshot()["analyzed_frames"]==1)
        saved=league.context;league.context=None
        wait_until(lambda:worker.snapshot()["status"]=="waiting for fresh supported match data")
        assert worker.isRunning()
        league.context=saved;worker.stream.put(2)
        wait_until(lambda:worker.snapshot()["analyzed_frames"]==2)
        league.context=replace(saved,epoch=2)
        wait_until(lambda:not worker.isRunning())
        assert not worker.dispatcher.current
    finally:worker.stop();assert worker.wait(2000)


@pytest.fixture
def visual_video(visual_fixture):
    spec,images,frame,root=visual_fixture
    video=root/"synthetic.avi";writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*"MJPG"),10,(128,128))
    assert writer.isOpened()
    for i in range(30):writer.write(frame(5<=i<=20))
    writer.release()
    cap=cv2.VideoCapture(str(video));cap.set(cv2.CAP_PROP_POS_FRAMES,6);ok,decoded=cap.read();cap.release();assert ok
    cv2.imwrite(str(root/"Lee Sin.png"),decoded[20:36,20:36]);cv2.imwrite(str(root/"Ahri.png"),decoded[90:106,90:106])
    return video,root/"calibration.json",root/"analysis"


def test_recording_review_and_exhaustive_evaluation_are_distinct(visual_video):
    video,calibration,output=visual_video
    report=analyze_recording(video,calibration,output)
    assert report["analyzed_frames"]==30 and len(report["candidates"])==1
    assert report["accuracy"] is None and not (output/"debug").exists()
    assert "minimap_cache" not in report and not list(output.glob("*.npy"))
    candidate=report["candidates"][0]
    assert candidate["first_seen_s"]==pytest.approx(.5) and candidate["confirmed_at_s"]==pytest.approx(.7)
    labels=new_labels(video,calibration);labels["kind"]="synthetic";labels["coverage"]["end_s"]=2.9
    labels["reviews"]=[{"candidate_id":candidate["id"],"decision":"confirmed"}]
    save_json(output/"labels.json",labels)
    assert evaluate_labels(output/"detections.json",output/"labels.json")["accuracy"] is None
    labels["annotation_complete"]=True
    labels["labels"]=[{"champion":"Lee Sin","source":"minimap","start_s":.5,"end_s":2.,"position":[28/128,28/128],"near_player":False}]
    save_json(output/"labels.json",labels)
    scored=evaluate_labels(output/"detections.json",output/"labels.json")
    assert scored["real_footage_accuracy"] is None
    assert scored["accuracy"]["true_positive_frames"]==14 and scored["accuracy"]["false_negative_frames"]==2
    assert scored["accuracy"]["false_positive_frames"]==0 and scored["accuracy"]["champion_identity_accuracy"]==1
    assert scored["accuracy"]["latency"]["appearance_to_temporally_confirmed_sighting"]["p50_ms"]==pytest.approx(200)
    with pytest.raises(ValueError,match="fresh"):analyze_recording(video,calibration,output)


def test_label_missing_positives_wrong_identity_and_invalid_inputs(visual_video):
    video,calibration,output=visual_video;analyze_recording(video,calibration,output)
    labels=new_labels(video,calibration);labels.update(kind="synthetic",annotation_complete=True)
    labels["coverage"]["end_s"]=2.9
    labels["labels"]=[{"champion":"Garen","source":"minimap","start_s":.5,"end_s":2.,"position":[28/128,28/128]}]
    save_json(output/"labels.json",labels);score=evaluate_labels(output/"detections.json",output/"labels.json")["accuracy"]
    assert score["true_positive_frames"]==0 and score["false_positive_frames"]==14 and score["false_negative_frames"]==16
    assert score["champion_identity_accuracy"]==0 and score["false_positive_episodes"]==1
    for change in ({"coverage":{"start_s":2,"end_s":1}},{"labels":[{"champion":"X"}]},{"complete_for":["fog"]}):
        with pytest.raises(ValueError):validate_labels({**labels,**change})


def test_cancelled_video_does_not_produce_a_success_report(visual_video):
    from core.cancellation import OperationCancelled
    video,calibration,output=visual_video;cancel=Event();cancel.set()
    with pytest.raises(OperationCancelled):analyze_recording(video,calibration,output,cancel)
    assert not (output/"detections.json").exists()


def test_explicit_recorded_pixel_cache_reproduces_detections_and_validates_geometry(visual_video):
    from core.league_visual_evaluation import analyze_minimap_cache
    video,calibration,output=visual_video
    report=analyze_recording(video,calibration,output,cache_minimap=True)
    cache=report["minimap_cache"]
    assert cache["shape"]==[30,128,128,3]
    assert np.load(output/cache["pixels"],allow_pickle=False).dtype==np.uint8
    cached=output/"repeat"
    repeated=analyze_minimap_cache(output/"detections.json",calibration,cached)
    assert (output/"frames.jsonl").read_bytes()==(cached/"frames.jsonl").read_bytes()
    assert repeated["candidates"]==report["candidates"]
    assert repeated["metrics"]["visual_processing"]["count"]==30
    with pytest.raises(ValueError,match="fresh"):
        analyze_minimap_cache(output/"detections.json",calibration,cached)
    changed=read_saved(calibration);changed["minimap"]=[0,0,.9,1]
    different=calibration.with_name("different.json");save_json(different,changed)
    with pytest.raises(ValueError,match="geometry"):
        analyze_minimap_cache(output/"detections.json",different,output/"wrong")
    changed=read_saved(calibration);changed["fps"]=5;save_json(different,changed)
    with pytest.raises(ValueError,match="cadence"):
        analyze_minimap_cache(output/"detections.json",different,output/"wrong-rate")


def test_real_policy_harness_reuses_identical_raw_evidence_and_simulation_never_calls_audio(visual_video,monkeypatch):
    from core.league_visual_evaluation import compare_confirmation_policies,simulate_visual_notifications
    video,calibration,output=visual_video
    analyze_recording(video,calibration,output,cache_minimap=True)
    def forbidden(*args,**kwargs):raise AssertionError("No speech in offline policy evaluation")
    monkeypatch.setattr("voice.tts.speak",forbidden)
    monkeypatch.setattr("core.model_client.request_chat",forbidden)
    compared=compare_confirmation_policies(output/"detections.json",calibration,output/"policies")
    assert set(compared["policies"])=={"strict3","two_validated","adaptive"}
    paths=[output/"policies"/p/"frames.jsonl" for p in compared["policies"]]
    rows=[[json.loads(line) for line in path.read_text().splitlines()] for path in paths]
    assert all([r["raw_observations"] for r in rows[0]]==[r["raw_observations"] for r in other] for other in rows[1:])
    simulated=simulate_visual_notifications(output/"policies"/"strict3"/"detections.json")
    assert simulated["scenarios"]["optimized_controlled_region"]["counts"]["started"]==1


def test_reference_ablations_cannot_expand_a_champion_limited_review_scope(visual_video):
    from benchmark_league import reference_variant_benchmark
    video,calibration,output=visual_video
    analyze_recording(video,calibration,output,cache_minimap=True)
    labels=new_labels(video,calibration);labels.update(kind="synthetic",annotation_complete=True,complete_champions=["Lee Sin"])
    labels["coverage"]["end_s"]=2.9
    labels["labels"]=[{"champion":"Lee Sin","source":"minimap","start_s":.5,"end_s":2.,"position":[28/128,28/128]}]
    truth=save_json(output/"lee-only.json",labels)
    manifest=save_json(output/"variants.json",{"Garen":{"working":"Garen.png"}})
    result=reference_variant_benchmark(output/"detections.json",calibration,output/"variant-test",manifest,[truth])
    assert result["variants"]["Garen:working"]["scores"]=={}


def test_cache_cancellation_and_malformed_timestamps_never_produce_success(visual_video):
    from core.league_visual_evaluation import analyze_minimap_cache
    from core.cancellation import OperationCancelled
    video,calibration,output=visual_video
    report=analyze_recording(video,calibration,output,cache_minimap=True)
    cancelled=output/"cancelled";event=Event();event.set();threads=cv2.getNumThreads()
    with pytest.raises(OperationCancelled):
        analyze_minimap_cache(output/"detections.json",calibration,cancelled,event)
    assert not (cancelled/"detections.json").exists() and cv2.getNumThreads()==threads
    np.save(output/report["minimap_cache"]["timestamps"],np.full(30,np.nan))
    with pytest.raises(ValueError,match="Malformed"):
        analyze_minimap_cache(output/"detections.json",calibration,output/"bad")


def test_review_windows_ignore_ambiguity_and_do_not_invent_onset_latency(visual_video):
    video,calibration,output=visual_video
    analyze_recording(video,calibration,output)
    labels=new_labels(video,calibration)
    labels.update(kind="synthetic",annotation_complete=True,complete_champions=["Lee Sin","Garen"],
                  reviewer={"type":"fixture","independent_human_review":False})
    labels["coverage"]={"start_s":0.,"end_s":2.9,"intervals":[[0.,.4],[1.,1.2],[2.5,2.9]]}
    labels["labels"]=[{"champion":"Lee Sin","source":"minimap","start_s":1.,"end_s":1.2,
                       "position":[28/128,28/128],"onset_known":False,"episode_id":"left-censored"}]
    labels["ignored_regions"]=[{"source":"minimap","start_s":1.1,"end_s":1.2,"bounds":[0,0,.5,.5]}]
    path=output/"audit.json";save_json(path,labels)
    score=evaluate_labels(output/"detections.json",path);a=score["accuracy"]
    assert a["true_positive_frames"]==1 and a["false_positive_frames"]==a["false_negative_frames"]==0
    assert a["evaluated_frames"]==13 and a["evaluated_source_seconds"]==1.3
    assert a["ignored_prediction_frames"]==2 and not a["latency"]
    assert a["per_champion"]["Garen"]["recall"] is None
    assert not score["annotation_provenance"]["independent_human_review"]


def test_moving_labels_share_one_known_onset_and_champion_scopes_are_explicit(visual_video):
    video,calibration,output=visual_video;analyze_recording(video,calibration,output)
    labels=new_labels(video,calibration);labels.update(kind="synthetic",annotation_complete=True)
    labels["coverage"]["end_s"]=2.9
    common={"champion":"Lee Sin","source":"minimap","position":[28/128,28/128],"episode_id":"one"}
    labels["labels"]=[{**common,"start_s":.5,"end_s":.6,"onset_known":True,"onset_s":.5},
                       {**common,"start_s":.7,"end_s":2.,"onset_known":False}]
    path=output/"labels.json";save_json(path,labels)
    score=evaluate_labels(output/"detections.json",path)["accuracy"]
    assert score["known_onset_episodes"]==1 and score["detected_label_intervals"]==1
    assert score["latency"]["appearance_to_temporally_confirmed_sighting"]["p50_ms"]==pytest.approx(200)
    labels.update(complete_champions=["Garen"],labels=[]);save_json(path,labels)
    score=evaluate_labels(output/"detections.json",path)["accuracy"]
    assert score["false_positive_frames"]==0 and score["reviewed_champions"]==["Garen"]
    labels["labels"]=[{**common,"start_s":.5,"end_s":2.}];save_json(path,labels)
    with pytest.raises(ValueError,match="scope"):evaluate_labels(output/"detections.json",path)


def test_equivalent_geometry_requires_explicit_opt_in_and_never_accepts_different_roi(visual_video):
    video,calibration,output=visual_video;analyze_recording(video,calibration,output)
    labels=new_labels(video,calibration);labels["coverage"]["end_s"]=2.9
    alternate=calibration.with_name("alternate.json");save_json(alternate,{**read_saved(calibration),"threshold":.88})
    labels["calibration"]=str(alternate);path=output/"labels.json";save_json(path,labels)
    with pytest.raises(ValueError,match="different calibrations"):evaluate_labels(output/"detections.json",path)
    assert evaluate_labels(output/"detections.json",path,allow_equivalent_calibration=True)["accuracy"] is None
    save_json(alternate,{**read_saved(alternate),"minimap":[0,0,.9,1]})
    with pytest.raises(ValueError,match="different calibrations"):
        evaluate_labels(output/"detections.json",path,allow_equivalent_calibration=True)


@pytest.mark.parametrize("span",[[[1,2],[1.5,2.5]],[[2,1]],[[0,3.1]],[]])
def test_invalid_disjoint_review_coverage_is_rejected(span):
    labels=new_labels();labels["coverage"]={"start_s":0.,"end_s":3.,"intervals":span}
    with pytest.raises(ValueError,match="intervals"):validate_labels(labels)


def test_live_service_is_dormant_and_approval_cannot_be_bypassed(visual_fixture,qapp,monkeypatch):
    from core.league import LeagueClient
    from gui.league_vision import LeagueVisualService,live_allowed
    league=LeagueClient();service=LeagueVisualService(league)
    service.sync();assert service.worker is None and service.status()["status"]=="disabled"
    monkeypatch.setattr(config,"LEAGUE_VISION_ENABLED",True)
    monkeypatch.setattr(league,"visual_context",lambda:LaneContext(supported=True))
    assert not live_allowed(league)
    service.sync();assert service.worker is None
    service.close();assert league.visual_announcement_current is None


def test_calibration_review_gui_roundtrip_and_decoder_shutdown(visual_video,qapp,wait_until,monkeypatch):
    from gui.league_calibration import LeagueCalibrationDialog
    video,calibration,output=visual_video;report=analyze_recording(video,calibration,output)
    dialog=LeagueCalibrationDialog(calibration,str(video),detections=str(output/"detections.json"),labels=str(output/"labels.json"))
    dialog.show();dialog.table.selectRow(0)
    wait_until(lambda:dialog.worker is None and dialog.frame is not None)
    dialog.confirm();assert dialog.manifest["reviews"][0]["decision"]=="confirmed"
    assert len(dialog.manifest["labels"])==1
    dialog.kind.setCurrentText("synthetic");dialog.save_labels()
    assert read_saved(output/"labels.json")["annotation_complete"] is False
    dialog.reject();assert dialog.manifest["reviews"][0]["decision"]=="rejected"
    assert dialog.manifest["labels"]==[]
    monkeypatch.setattr("gui.league_calibration.inference_priority.background_ready",lambda:True)
    dialog.generate()
    wait_until(lambda:dialog.worker is None)
    assert "review-" in str(dialog.detections_path)
    dialog.evaluate()
    wait_until(lambda:dialog.worker is None)
    assert (output/"labels-accuracy.json").is_file()
    dialog.at.setValue(1);dialog.load_frame();dialog.close()
    wait_until(lambda:dialog.worker is None and not dialog.isVisible())
    dialog.deleteLater()


def read_saved(path):return json.loads(path.read_text(encoding="utf-8"))


def test_accuracy_rejects_changed_recording_or_calibration(visual_video):
    video,calibration,output=visual_video;analyze_recording(video,calibration,output)
    labels=new_labels(video,calibration);labels["coverage"]["end_s"]=2.9
    save_json(output/"labels.json",labels)
    original=calibration.read_text();calibration.write_text(original+" ")
    with pytest.raises(ValueError,match="Calibration changed"):evaluate_labels(output/"detections.json",output/"labels.json")
    calibration.write_text(original)
    with video.open("ab") as stream:stream.write(b"changed")
    with pytest.raises(ValueError,match="Recording changed"):evaluate_labels(output/"detections.json",output/"labels.json")


def test_controlled_test_tone_is_cached_and_uses_existing_playback_only(tmp_path,monkeypatch):
    import wave
    from core.league_visual_sound import measure_test_sound
    paths=[]
    def playback(path,cancellation):
        with wave.open(str(path),"rb") as audio:
            assert audio.getframerate()==24000 and audio.getnchannels()==1 and audio.getnframes()==1080
        assert not cancellation.is_set();paths.append(path)
    monkeypatch.setattr("voice.tts._play_wav",playback)
    result=measure_test_sound(2)
    assert result["samples"]==2 and len(paths)==2 and paths[0]==paths[1]

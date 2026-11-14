import copy
import json
import time
from threading import Event,Thread

import numpy as np
import pytest

cv2=pytest.importorskip("cv2",reason="Optional replay dependency unavailable")

import config
from core.cancellation import OperationCancelled
from core.league import LeagueClient
from core.league_replay import MinimapDetector,SightingTracker,analyze_video,review_report,load_report,timeline,local_file


@pytest.fixture
def calibration(tmp_path):
    rng=np.random.default_rng(123)
    images={name:np.repeat(rng.integers(20,240,(16,16,1),dtype=np.uint8),3,axis=2) for name in ("Lee Sin","Ahri")}
    for name,image in images.items():
        cv2.imwrite(str(tmp_path/f"{name}.png"),image)
    spec={"reference_size":[128,128],"minimap":[0,0,1,1],"perspective":"player","fog_of_war":"player",
          "roster":[{"champion":"Lee Sin","side":"enemy","position":"JUNGLE"},{"champion":"Ahri","side":"ally","position":"MIDDLE"}],
          "templates":{name:f"{name}.png" for name in images},"threshold":.9,"margin":.08,
          "regions":[{"label":"top lane region","bounds":[0,0,.5,.5]},{"label":"bot lane region","bounds":[.5,.5,.5,.5]}]}
    (tmp_path/"calibration.json").write_text(json.dumps(spec))
    return spec,images,tmp_path


def frame(images,target=True):
    image=np.zeros((128,128,3),dtype=np.uint8)
    image[90:106,90:106]=images["Ahri"]
    if target:
        image[20:36,20:36]=images["Lee Sin"]
    return image


def test_visible_icons_and_scaling_are_grounded_to_calibrated_pixels(calibration):
    spec,images,root=calibration
    detector=MinimapDetector(spec,root)
    found,_=detector.detect(frame(images))
    assert found["champion"]=="Lee Sin" and found["region"]=="top lane region"
    assert found["template_similarity"]>.99
    resized=cv2.resize(frame(images),(256,256),interpolation=cv2.INTER_NEAREST)
    other,_=detector.detect(resized)
    assert other["minimap_position"]==found["minimap_position"]
    assert detector.detect(frame(images,False))[0] is None


def test_wrong_icons_low_confidence_and_ambiguous_identical_portraits_are_rejected(calibration):
    spec,images,root=calibration
    detector=MinimapDetector(spec,root)
    wrong=frame(images,False)
    wrong[20:36,20:36]=images["Ahri"]
    assert detector.detect(wrong)[0] is None
    occluded=frame(images)
    occluded[20:30,20:36]=0
    assert detector.detect(occluded)[0] is None
    ambiguous=copy.deepcopy(spec)
    ambiguous["templates"]["Ahri"]=ambiguous["templates"]["Lee Sin"]
    assert MinimapDetector(ambiguous,root).detect(frame(images))[0] is None


def test_cached_color_roster_comparison_matches_opencv_for_every_champion_and_scale(calibration):
    spec,images,root=calibration;detector=MinimapDetector(spec,root)
    rng=np.random.default_rng(87)
    for width,height in detector._competitors:
        for patch in [rng.integers(0,255,(height,width,3),dtype=np.uint8),
                      cv2.resize(images["Lee Sin"],(width,height)),np.zeros((height,width,3),np.uint8)]:
            actual=detector.roster_scores(patch)
            for name,reference in detector._competitors[width,height].items():
                expected=float(cv2.matchTemplate(patch,reference,cv2.TM_CCOEFF_NORMED)[0,0])
                assert actual[name]==pytest.approx(expected,abs=2e-5)


@pytest.mark.parametrize("change",[{"perspective":"spectator"},{"fog_of_war":"omniscient"},{"minimap":[-.1,0,.5,.5]},
                                  {"reference_size":[1,2]},{"threshold":.1},{"target_champion":"Ahri"}])
def test_incorrect_or_non_player_calibration_fails(calibration,change):
    spec,_,root=calibration
    with pytest.raises(ValueError):
        MinimapDetector({**spec,**change},root)


def test_flat_color_templates_cannot_match_every_position(calibration):
    spec,_,root=calibration
    cv2.imwrite(str(root/"Lee Sin.png"),np.full((16,16,3),(0,0,255),dtype=np.uint8))
    with pytest.raises(ValueError,match="textured"):
        MinimapDetector(spec,root)


def test_temporal_confirmation_dedup_and_no_positions_during_absence(calibration):
    spec,images,root=calibration
    found,_=MinimapDetector(spec,root).detect(frame(images))
    tracker=SightingTracker()
    assert tracker.update(found,0) is None
    candidate=tracker.update(found,.5)
    assert candidate["video_time_s"]==0 and "human review required" in candidate["uncertainty"]
    for second in (1,1.5,2,3):
        assert tracker.update(found,second) is None
    assert tracker.update(None,4) is None
    assert tracker.update(None,5) is None  # Fog/absence never produces a location.
    assert tracker.update(found,6) is None
    assert tracker.update(found,6.5)["video_time_s"]==6


@pytest.fixture
def recorded_video(calibration):
    spec,images,root=calibration
    path=root/"recorded.avi"
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*"MJPG"),4.,(128,128))
    assert writer.isOpened()
    for index in range(20):
        writer.write(frame(images,2<=index<=8 or index>=16))
    writer.release()
    # Calibrate from decoded footage rather than assuming an uncompressed asset
    # is identical to a compressed minimap portrait.
    cap=cv2.VideoCapture(str(path));cap.set(cv2.CAP_PROP_POS_FRAMES,2)
    ok,decoded=cap.read();cap.release();assert ok
    cv2.imwrite(str(root/"Lee Sin.png"),decoded[20:36,20:36])
    cv2.imwrite(str(root/"Ahri.png"),decoded[90:106,90:106])
    return path,root/"calibration.json",root/"analysis"


def test_actual_video_analysis_timeline_requires_review_and_records_uncertainty(recorded_video):
    video,calibration,output=recorded_video
    report=analyze_video(video,calibration,output)
    assert report["analyzed_frames"]==10 and len(report["candidates"])==2
    assert report["confirmed_sightings"]==[] and "No human-confirmed" in timeline(report)
    assert [item["video_time_s"] for item in report["candidates"]]==[.5,4.]
    assert all((output/item["evidence_image"]).is_file() for item in report["candidates"])
    identity=report["candidates"][0]["id"]
    reviewed=review_report(output/"report.json",[identity],[identity])
    assert len(reviewed["confirmed_sightings"])==1 and reviewed["confirmed_sightings"][0]["player_marked_missed"] is True
    assert "user confirmed" in timeline(load_report(output/"report.json"))
    with pytest.raises(ValueError):
        review_report(output/"report.json",["invented"])
    with pytest.raises(ValueError):
        review_report(output/"report.json",[],[identity])


def test_analysis_cancellation_releases_video_and_never_writes_a_success_report(recorded_video):
    video,calibration,output=recorded_video
    stop=Event();stop.set()
    with pytest.raises(OperationCancelled):
        analyze_video(video,calibration,output,stop)
    assert not (output/"report.json").exists()
    for path in ("https://example.com/live","0","\\\\host\\live.mp4"):
        with pytest.raises(ValueError):
            local_file(path)


def pump(qapp,predicate):
    deadline=time.monotonic()+3
    while not predicate() and time.monotonic()<deadline:
        qapp.processEvents();time.sleep(.005)
    assert predicate()


def test_gui_job_is_optional_single_queued_nonblocking_and_does_not_speak(calibration,qapp,monkeypatch):
    from gui.league_replay import ReplayService
    _,_,root=calibration
    video=root/"recorded.mp4";video.write_bytes(b"fixture")
    league=LeagueClient();service=ReplayService(league)
    assert service.submit(video,root/"calibration.json")["status"]=="disabled"
    monkeypatch.setattr(config,"LEAGUE_REPLAY_ENABLED",True)
    gate=Event()
    monkeypatch.setattr("gui.league_replay.inference_priority.background_ready",gate.is_set)
    results=[]
    monkeypatch.setattr("core.league_replay.analyze_video",lambda *args:results.append(True) or {"candidates":[],"confirmed_sightings":[]})
    accepted=[]
    thread=Thread(target=lambda:accepted.append(service.submit(video,root/"calibration.json")))
    thread.start();thread.join()
    assert accepted[0]["status"]=="queued"
    assert service.submit(video,root/"calibration.json")["status"]=="busy"
    pump(qapp,lambda:service.worker is not None)
    assert not results  # Foreground work/grace period wins admission.
    gate.set()
    pump(qapp,lambda:service.worker is None)
    assert results==[True] and service.status()["status"]=="complete"
    service.close()


def test_new_match_and_shutdown_cancel_an_offline_job(calibration,qapp,monkeypatch):
    from gui.league_replay import ReplayService
    _,_,root=calibration
    video=root/"video.avi";video.write_bytes(b"fixture")
    monkeypatch.setattr(config,"LEAGUE_REPLAY_ENABLED",True)
    league=LeagueClient();service=ReplayService(league)
    league._session.active=True
    assert service.submit(video,root/"calibration.json")["status"]=="unavailable"
    league._session.active=False
    monkeypatch.setattr("gui.league_replay.inference_priority.background_ready",lambda:False)
    service.submit(video,root/"calibration.json")
    pump(qapp,lambda:service.worker is not None)
    service.close()
    pump(qapp,lambda:service.worker is None)
    assert service.status()["status"]=="cancelled"

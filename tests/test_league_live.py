"""Phase 7 permissions, actual-roster preparation and existing worker lifecycle."""
import copy
import time
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

import config
from core.league_lane import LaneContext,LaneTracker
from core.league_visual import VisualCalibration
from core.league_visual_modes import VisionMode,speech_permission,prepare_live_profile,roster_key,configured_mode
from core.league_visual_evaluation import save_json,file_digest
from gui.league_vision import LeagueVisualWorker,LeagueVisualService,live_allowed


@pytest.fixture
def setup(tmp_path):
    import cv2
    roster=tuple({"champion":f"{'Ally' if i<5 else 'Enemy'}{i}","side":"ally" if i<5 else "enemy"} for i in range(10))
    sources=tmp_path/"riot-unit";sources.mkdir();rng=np.random.default_rng(7)
    for p in roster:cv2.imwrite(str(sources/(p["champion"]+".png")),rng.integers(0,255,(64,64,3),dtype=np.uint8))
    spec={"reference_size":[128,128],"minimap":[0,0,1,1],"perspective":"player","fog_of_war":"player",
          "game_mode":"CLASSIC","map_number":11,"roster":[{"champion":"Old","side":"enemy"}],
          "templates":{"Old":"old-recorded.png"},"assigned_role":"UTILITY","player_champion":"Yuumi",
          "regions":[{"label":"old recorded lane","bounds":[0,0,1,1],"lane":"BOTTOM"}]}
    path=save_json(tmp_path/"calibration.json",spec)
    class Knowledge:
        version="unit"
        calls=[]
        def champion_icon(self,name,*args):self.calls.append(name);return sources/(name+".png")
    state=LaneContext("TOP","live API","TOP","CLASSIC",11,"ORDER","Ally0",1,True)
    league=SimpleNamespace(knowledge=Knowledge(),roster=lambda:roster,session_active=True,visual_context=lambda:state)
    return path,league,state,roster


def test_live_profile_uses_all_current_names_and_official_cached_refs_not_recorded_roster(setup):
    path,league,state,roster=setup;before=path.read_bytes()
    profile=prepare_live_profile(path,league,state,Event())
    assert roster_key(profile.spec["roster"])==roster_key(roster)
    assert len(profile.spec["templates"])==10 and set(league.knowledge.calls)=={p["champion"] for p in roster}
    assert profile.spec["assigned_role"]=="TOP" and profile.spec["player_champion"]=="Ally0"
    assert profile.spec["confirmation_policy"]=="strict3" and not profile.regions
    assert path.read_bytes()==before
    changed=replace(state,role="BOTTOM",player_champion="Ally2")
    again=prepare_live_profile(path,league,changed,Event())
    assert len(league.knowledge.calls)==10  # No repeated static downloads.
    assert again.spec["player_champion"]=="Ally2" and again.spec["assigned_role"]=="BOTTOM"


def test_changed_roster_rebuilds_only_missing_cached_references(setup):
    path,league,state,roster=setup
    first=prepare_live_profile(path,league,state,Event())
    changed=[dict(p) for p in roster];changed[0]["champion"]="Enemy9"
    league.roster=lambda:tuple(changed)
    other=prepare_live_profile(path,league,state,Event())
    assert roster_key(other.spec["roster"])!=roster_key(first.spec["roster"])
    assert len(list(first.directory.glob("*.json")))==2
    assert "Mirror" in other.spec["recognition_blocked_reason"]


def test_practice_without_enemy_keeps_capture_profile_without_guessing_roster_or_player_position(setup):
    path,league,state,roster=setup;league.roster=lambda:roster[:1]
    context=replace(state,role=None,assigned_lane=None,mode="PRACTICETOOL")
    profile=prepare_live_profile(path,league,context,Event())
    assert len(profile.spec["roster"])==1 and not profile.spec["templates"]
    assert "Waiting" in profile.spec["recognition_blocked_reason"]
    assert profile.spec["assigned_role"]=="UNKNOWN"
    tracker=LaneTracker();tracker.update(1,"PRACTICETOOL",11,"NONE","ORDER","Yuumi")
    assert tracker.snapshot().supported and tracker.snapshot().assigned_lane is None


@pytest.mark.parametrize("roster",[[],[None],[{"champion":"","side":"enemy"}],[{"champion":"A","side":"unknown"}]])
def test_invalid_api_roster_cannot_load_recorded_champions(setup,roster):
    path,league,state,_=setup;league.roster=lambda:roster
    with pytest.raises(ValueError):prepare_live_profile(path,league,state,Event())


def test_configuration_never_proves_riot_approval_or_private_environment(monkeypatch):
    monkeypatch.setattr(config,"LEAGUE_VISION_RIOT_APPROVED",True)
    assert not speech_permission(VisionMode.APPROVED_ALERTS)[0]
    assert not speech_permission(VisionMode.LIVE_OBSERVATION)[0]
    assert not speech_permission(VisionMode.CONTROLLED_TEST,replay_valid=False,now=1,deadline=2)[0]
    assert not speech_permission(VisionMode.CONTROLLED_TEST,replay_valid=True,session_active=True,now=1,deadline=2)[0]
    assert not speech_permission(VisionMode.CONTROLLED_TEST,replay_valid=True,now=2,deadline=2)[0]
    assert speech_permission(VisionMode.CONTROLLED_TEST,replay_valid=True,now=1,deadline=2)[0]


def test_environment_cannot_silently_opt_into_controlled_speech(monkeypatch):
    monkeypatch.setattr(config,"LEAGUE_VISION_MODE","CONTROLLED_TEST")
    assert configured_mode()==VisionMode.LIVE_OBSERVATION


def test_silent_observation_does_not_require_an_approval_flag(setup,monkeypatch):
    _,league,_,_=setup
    monkeypatch.setattr(config,"LEAGUE_VISION_RIOT_APPROVED",False)
    assert live_allowed(league,VisionMode.LIVE_OBSERVATION)
    assert not live_allowed(league,VisionMode.OFF)
    league.session_active=False
    assert not live_allowed(league,VisionMode.LIVE_OBSERVATION)


def test_service_mode_changes_wait_for_fresh_match_and_recorded_test_requires_opt_in(setup,qapp):
    _,league,_,_=setup;league.session_active=False
    service=LeagueVisualService(league)
    service.request_mode(VisionMode.LIVE_OBSERVATION)
    assert service.worker is None and service.status()["mode"]=="LIVE_OBSERVATION"
    service.request_mode(VisionMode.APPROVED_ALERTS)
    assert service.status()["mode"]=="LIVE_OBSERVATION" and not service.status()["speech_enabled"]
    assert "not approval" in service.status()["speech_disabled_reason"]
    with pytest.raises(ValueError):service.start_replay("a","b","c")
    league.session_active=True
    with pytest.raises(ValueError):service.start_replay("a","b","c",opt_in=True)
    service.close()


def test_observation_worker_is_silent_even_if_legacy_approval_flag_is_true(tmp_path,qapp,wait_until,monkeypatch):
    import cv2
    rng=np.random.default_rng(1);a=rng.integers(0,255,(16,16,3),dtype=np.uint8);b=np.flip(a,axis=0).copy()
    cv2.imwrite(str(tmp_path/"A.png"),a);cv2.imwrite(str(tmp_path/"B.png"),b)
    spec={"reference_size":[128,128],"minimap":[0,0,1,1],"perspective":"player","fog_of_war":"player",
        "roster":[{"champion":"A","side":"enemy"},{"champion":"B","side":"ally"}],"templates":{"A":"A.png","B":"B.png"},
        "game_mode":"CLASSIC","map_number":11,"assigned_role":"MIDDLE","threshold":.9,"alerts_muted":False,
        "regions":[{"label":"mid","bounds":[0,0,1,1],"lane":"MIDDLE","verification":{"kind":"operator_confirmed","frame_s":0}}]}
    path=save_json(tmp_path/"profile.json",spec);profile=VisualCalibration(spec,tmp_path)
    state=profile.offline_lane();league=SimpleNamespace(session_active=True,visual_context=lambda:state,roster=lambda:spec["roster"])
    monkeypatch.setattr("gui.league_vision.prepare_live_profile",lambda *a:profile)
    monkeypatch.setattr(config,"LEAGUE_VISION_RIOT_APPROVED",True)
    monkeypatch.setattr("gui.league_vision.inference_priority.background_ready",lambda:True)
    from contextlib import contextmanager
    from core.league_capture import RegionFrame
    class Capture:
        sequence=0
        @contextmanager
        def open(self):
            def grab():
                self.sequence+=1;image=np.zeros((128,128,3),np.uint8);image[30:46,30:46]=a
                return RegionFrame({"minimap":image},time.monotonic(),self.sequence,"test",())
            yield grab
    monkeypatch.setattr("core.league_capture.GameWindowCapture",lambda *args:Capture())
    worker=LeagueVisualWorker(league,path);notifications=[];worker.notification.connect(notifications.append)
    worker.start()
    try:
        wait_until(lambda:worker.snapshot()["confirmed_sightings"]>0)
        assert not notifications and worker.snapshot()["notifications"]["proposed"]==0
        assert len(worker.recent)<=64
    finally:worker.stop();assert worker.wait(2000)


def test_status_panel_uses_aggregate_counts_and_never_displays_enemy_coordinates(setup,qapp):
    from gui.league_vision_panel import LeagueVisionPanel
    _,league,_,_=setup;league.session_active=False
    service=LeagueVisualService(league);panel=LeagueVisionPanel(service)
    panel.refresh()
    assert "OFF" in panel.status.text() and "Confirmed sighting episodes" in panel.status.text()
    assert not panel.opt_in.isChecked() and not panel.assistant.isChecked()
    panel.start_test();assert "opt-in" in panel.message.text()
    panel.close();service.close()


@pytest.mark.parametrize("available,alpha,flags,expected",[(False,0,0,False),(True,255,2,False),
                                                        (True,0,0,False),(True,0,2,True)])
def test_only_verified_global_zero_alpha_can_skip_an_overlay(available,alpha,flags,expected):
    from core.league_capture import GameWindowCapture
    capture=object.__new__(GameWindowCapture);capture.dwm=None
    def attributes(handle,color,a,f):a._obj.value=alpha;f._obj.value=flags;return available
    capture.api=SimpleNamespace(GetLayeredWindowAttributes=attributes)
    assert capture.demonstrably_invisible(1) is expected


def test_recorded_controlled_test_uses_existing_confirmation_and_resets_on_end(tmp_path,qapp,wait_until,monkeypatch):
    import cv2
    from benchmarks.league_vision_e2e import fingerprint
    rng=np.random.default_rng(33);a=rng.integers(20,240,(16,16,3),dtype=np.uint8);b=np.flip(a,axis=0).copy()
    video=tmp_path/"local.avi";writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*"MJPG"),10,(128,128))
    assert writer.isOpened()
    for i in range(30):
        image=np.zeros((128,128,3),np.uint8);image[90:106,90:106]=b
        if 5<=i<=24:image[30:46,30:46]=a
        writer.write(image)
    writer.release();cap=cv2.VideoCapture(str(video));cap.set(cv2.CAP_PROP_POS_FRAMES,7);ok,image=cap.read();cap.release();assert ok
    cv2.imwrite(str(tmp_path/"A.png"),image[30:46,30:46]);cv2.imwrite(str(tmp_path/"B.png"),image[90:106,90:106])
    spec={"reference_size":[128,128],"minimap":[0,0,1,1],"perspective":"player","fog_of_war":"player",
        "roster":[{"champion":"A","side":"enemy"},{"champion":"B","side":"ally"}],"templates":{"A":"A.png","B":"B.png"},
        "game_mode":"CLASSIC","map_number":11,"assigned_role":"MIDDLE","threshold":.9,"alerts_muted":True,
        "regions":[{"label":"mid","bounds":[0,0,1,1],"lane":"MIDDLE","verification":{"kind":"operator_confirmed","frame_s":0}}]}
    path=save_json(tmp_path/"calibration.json",spec)
    manifest=save_json(tmp_path/"whitelist.json",{"kind":"operator_confirmed_replay_eligibility","video":str(video),
        "video_fingerprint":fingerprint(video),"calibration_sha256":file_digest(path),"reviewer":{"kind":"operator_confirmed"},
        "selected_sightings":[{"champion":"A","source":"minimap","position":[38/128,38/128],"start_s":.5,"end_s":2.4}]})
    league=SimpleNamespace(session_active=False,tracker=SimpleNamespace(epoch=0),visual_context=lambda:None)
    monkeypatch.setattr("gui.league_vision.inference_priority.background_ready",lambda:True)
    worker=LeagueVisualWorker(league,path,VisionMode.CONTROLLED_TEST,{"video":video,"manifest":manifest,"opt_in":True,"seconds":5})
    notices=[]
    def accept(ann):notices.append(ann);worker.announcement_status(ann,"started")
    worker.notification.connect(accept);worker.start()
    try:
        wait_until(lambda:len(notices)>0)
        assert worker.can_speak() and worker.announcement_current(notices[0])
        league.session_active=True  # A new actual match cancels the replay immediately.
        wait_until(lambda:not worker.isRunning())
        assert not worker.announcement_current(notices[0]) and notices[0].cancel_event.is_set()
        assert len(notices)==1
    finally:worker.stop();assert worker.wait(2000)


def test_visual_notification_epoch_is_rechecked_before_speech(tmp_path,qapp):
    from core.league_voice import LeagueAnnouncement
    league=SimpleNamespace(session_active=False,tracker=SimpleNamespace(epoch=2),visual_context=lambda:None)
    worker=LeagueVisualWorker(league,tmp_path/"unused",VisionMode.CONTROLLED_TEST,{"opt_in":True})
    worker.replay_valid=True;worker.deadline=time.monotonic()+10
    worker.dispatcher=SimpleNamespace(is_current=lambda a:True,invalidate=lambda:None)
    ann=LeagueAnnouncement("test","visual:test",time.monotonic(),1)
    assert worker.announcement_current(ann)
    league.tracker.epoch=3
    assert not worker.announcement_current(ann)


def test_cached_hwnd_reuse_by_another_pid_is_rejected_before_pixel_capture():
    from core.league_capture import GameWindowCapture
    capture=object.__new__(GameWindowCapture)
    capture.allowed=lambda:True;capture.handle=7;capture.pid=42;capture._scan_at=0
    def pid(handle,value):value._obj.value=43;return 1
    capture.api=SimpleNamespace(GetForegroundWindow=lambda:7,IsWindow=lambda h:True,GetWindowThreadProcessId=pid)
    with pytest.raises(RuntimeError,match="identity changed"):capture.geometry()
    assert capture.handle is None and capture.pid is None


def test_current_roster_ignores_practice_tool_blank_champion_dummy():
    import asyncio
    import httpx
    from core.league import LeagueClient
    players=[{"riotId":"local#test","championName":"Yuumi","team":"ORDER","position":"NONE"},
             {"championName":"Sion","team":"CHAOS"},{"championName":"","team":"CHAOS"}]
    payloads={"activeplayer":{"riotId":"local#test","level":1,"championStats":{}},"playerlist":players,
              "gamestats":{"gameTime":10,"gameMode":"PRACTICETOOL","mapNumber":11}}
    client=LeagueClient()
    def get(request):return httpx.Response(200,json=payloads[request.url.path.rsplit("/",1)[-1]])
    async def refresh():
        async with httpx.AsyncClient(transport=httpx.MockTransport(get)) as http:
            await client.refresh_stats(http)
    asyncio.run(refresh())
    assert {p["champion"] for p in client.roster()}=={"Yuumi","Sion"}


def test_capture_context_change_accepts_a_restarted_frame_sequence(setup,monkeypatch):
    from core.live_screen import FrameBatch
    import core.league_visual as visual
    import core.league_capture as capture
    path,league,state,roster=setup
    profile=VisualCalibration({**VisualCalibration.load(path).spec,"roster":list(roster)},path.parent)
    monkeypatch.setattr("gui.league_vision.prepare_live_profile",lambda *args:profile)
    monkeypatch.setattr("gui.league_vision.inference_priority.background_ready",lambda:True)
    worker=LeagueVisualWorker(league,path)
    resets=[]
    class Pipeline:
        def __init__(self,profile):self.temporal=SimpleNamespace(reset=resets.append)
        def process(self,*args):return ()
    monkeypatch.setattr(visual,"LeagueVisualPipeline",Pipeline)
    monkeypatch.setattr(capture,"GameWindowCapture",lambda *args:SimpleNamespace(open=None))
    frames=iter([FrameBatch(i,(capture.RegionFrame({},time.monotonic(),1,"source",()),)) for i in (1,2)])
    class History:
        def __init__(self,**kwargs):pass
        def wait_sample(self,*args,**kwargs):
            try:return next(frames)
            except StopIteration:worker.stop();return FrameBatch(3,())
    class Stream:
        error=""
        def __init__(self,*args,**kwargs):pass
        def start(self):pass
        def stop(self):pass
        def join(self):pass
        def status(self):return {}
    monkeypatch.setattr("gui.league_vision.FrameHistory",History)
    monkeypatch.setattr("gui.league_vision.LiveScreenStream",Stream)
    worker.run()
    assert worker.stats["analyzed_frames"]==2 and len(resets)==2

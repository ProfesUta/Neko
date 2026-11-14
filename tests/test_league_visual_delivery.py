"""No League, model, microphone or output device: lifecycle and timing regressions."""
from dataclasses import replace
from threading import Event
import time

import pytest

from core.league_visual import VisualCalibration,VisualObservation,VisualAlertDispatcher,TemporalConfirmation
from core.league_voice import LeagueAnnouncement,LeagueVoicePolicy


@pytest.fixture
def profile(tmp_path):
    return VisualCalibration({"reference_size":[1920,1080],"minimap":[.8,.7,.19,.29],
        "roster":[{"champion":"Warwick","side":"enemy"},{"champion":"Karma","side":"enemy"},
                  {"champion":"Yuumi","side":"ally"},{"champion":"Caitlyn","side":"ally"}],
        "game_mode":"CLASSIC","map_number":11,"assigned_role":"UTILITY","player_champion":"Yuumi",
        "alerts_muted":False,"threshold":.85,"alert_cooldown":8},tmp_path)


def observation(at=100,**changes):
    return replace(VisualObservation("Warwick","enemy","mid",.97,True,at,"minimap",(.2,.2),(.16,.16,.08,.08),
                   in_assigned_lane=True,identity_margin=.4),**changes)


def test_rejected_proposal_spends_no_cooldown_and_continuous_started_sighting_is_never_repeated(profile):
    clock=[100.];d=VisualAlertDispatcher(profile)
    policy=LeagueVoicePolicy(clock=lambda:clock[0],on_status=lambda a,s:d.lifecycle(a,s,clock[0]))
    first=d.update([observation()],1,now=100)[0]
    important=LeagueAnnouncement("user priority","test",100,10,3)
    assert policy.offer(important) and not policy.offer(first)
    assert first.cancel_event.is_set() and d.cooldowns=={} and policy.last_started==float("-inf")
    clock[0]=101;policy.discard_pending()
    retry=d.update([observation(101)],1,now=101)[0]
    assert policy.offer(retry)
    taken=policy.take(True,defer_start=True)
    assert taken==retry and not d.cooldowns and policy.last_started==float("-inf")
    for at in (101.1,101.2,101.3,101.4):d.update([observation(at)],1,now=at)
    clock[0]=101.4
    policy.started(taken);policy.finish(taken)
    assert d.cooldowns=={"Warwick":101.4}
    # Keep genuinely continuous fresh observations through several cooldowns.
    for step in range(1,200):
        clock[0]=101.4+step*.1
        assert not d.update([observation(clock[0])],1,now=clock[0])
    assert d.counts["started"]==1 and d.counts["proposed"]==2
    d.update([],1,now=122)
    assert d.update([observation(122.1)],1,now=122.1)


def test_queue_replacement_before_playback_does_not_mark_a_start(profile):
    now=[100.];d=VisualAlertDispatcher(profile)
    policy=LeagueVoicePolicy(clock=lambda:now[0],on_status=lambda a,s:d.lifecycle(a,s,now[0]))
    one,two=d.update([observation(),observation(champion="Karma",position=(.7,.7))],1,now=100)
    assert policy.offer(one) and policy.offer(two)
    assert one.cancel_event.is_set() and not d.cooldowns
    assert d.states[two.event_key]["state"]=="accepted"
    taken=policy.take(True,defer_start=True);policy.finish(taken)
    assert two.cancel_event.is_set() and not d.cooldowns
    assert policy.last_started==float("-inf")


def test_pending_expiry_does_not_cancel_an_unrelated_inflight_speech():
    clock=[100.];changes=[]
    policy=LeagueVoicePolicy(clock=lambda:clock[0],on_status=lambda a,s:changes.append((a.event_key,s)))
    active=LeagueAnnouncement("one","active",100,10)
    policy.offer(active);policy.take(True,defer_start=True)
    policy.offer(LeagueAnnouncement("two","pending",100,.1))
    clock[0]=101;policy.take(False)
    assert policy.inflight==active and ("active","dropped") not in changes
    policy.finish(active);assert ("active","dropped") in changes


@pytest.mark.parametrize("transition",["absent","epoch","mute","stale","shutdown"])
def test_visibility_loss_cancels_synthesis_and_invalidates_queued_notifications(profile,transition):
    d=VisualAlertDispatcher(profile);ann=d.update([observation()],1,now=100)[0]
    assert d.lifecycle(ann,"accepted",100)
    if transition=="absent":d.update([],1,now=100.1)
    elif transition=="epoch":d.update([observation(100.1)],2,now=100.1)
    elif transition=="mute":d.muted=True;d.update([observation(100.1)],1,now=100.1)
    elif transition=="stale":d.update([observation()],1,now=101)
    else:d.invalidate()
    assert ann.cancel_event.is_set() and not d.is_current(ann,100.1)
    assert not d.cooldowns


def test_started_speech_also_cancels_when_its_visible_source_disappears(profile):
    d=VisualAlertDispatcher(profile);ann=d.update([observation()],1,now=100)[0]
    d.lifecycle(ann,"accepted",100);d.lifecycle(ann,"started",100)
    d.invalidate()
    assert ann.cancel_event.is_set() and d.cooldowns["Warwick"]==100


def test_brief_confirmation_dropout_debounces_only_spoken_notices_without_extending_visibility(profile):
    d=VisualAlertDispatcher(profile);ann=d.update([observation()],1,now=100)[0]
    d.lifecycle(ann,"started",100)
    for step in range(1,101):
        at=100+step*.1
        assert not d.update([observation(at)],1,now=at)
    for at in (110.1,110.2,110.3):
        assert not d.update([],1,now=at) and not d.current and not d.is_current(ann,at)
    assert not d.update([observation(110.4,first_seen_at=110.2)],1,now=110.4)
    d.update([],1,now=111)
    assert d.update([observation(111.1)],1,now=111.1)


def test_minimap_and_viewport_do_not_queue_two_notices_for_one_visible_champion(profile):
    profile.viewport_enabled=True  # Controlled verified-viewport fixture, no capture.
    d=VisualAlertDispatcher(profile)
    both=lambda at:[observation(at),observation(at,source="viewport",position=(.8,.8))]
    offered=d.update(both(100),1,now=100)
    assert len(offered)==1
    d.lifecycle(offered[0],"started",100)
    for i in range(1,100):assert not d.update(both(100+i*.1),1,now=100+i*.1)
    assert not d.update([observation(110,source="viewport",position=(.8,.8))],1,now=110)


@pytest.mark.parametrize("changes",[
    {"near_player":True,"distance":None},{"near_player":True,"distance":-.1},
    {"near_player":True,"distance":float("nan")},{"near_player":True,"distance":.05},
])
def test_unverified_player_proximity_never_qualifies_an_alert(profile,changes):
    d=VisualAlertDispatcher(profile)
    assert not d.update([observation(in_assigned_lane=None,**changes)],1,now=100)
    assert not d.cooldowns


@pytest.mark.parametrize("changes",[{"position":(float("nan"),.2)},{"position":(-1,.2)},
    {"position":(.2,2)},{"confidence":float("nan")},{"champion":"Caitlyn","team":"ally"}])
def test_invalid_or_allied_observations_never_generate_speech(profile,changes):
    assert not VisualAlertDispatcher(profile).update([observation(**changes)],1,now=100)


@pytest.mark.parametrize("policy,frames",[("strict3",3),("two_validated",2),("adaptive",2)])
def test_temporal_policy_minimum_evidence_and_stable_episode_origin(policy,frames):
    t=TemporalConfirmation(policy=policy)
    for i in range(frames):out=t.update([observation(i*.1)],i*.1,1)
    assert out and out[0].first_seen_at==0
    for i in range(frames,12):out=t.update([observation(i*.1)],i*.1,1)
    assert out[0].first_seen_at==0  # Origin must not slide with the deque.
    assert not t.update([],1.2,1)
    assert not t.update([observation(1.3)],1.3,1)
    assert not t.update([observation(1.4)],1.4,2)


@pytest.mark.parametrize("changes",[{"identity_margin":.1},{"confidence":.93},{"ambiguous":True}])
def test_adaptive_requires_three_samples_for_weak_or_occluded_evidence(changes):
    t=TemporalConfirmation(policy="adaptive")
    assert not t.update([observation(0,**changes)],0,1)
    assert not t.update([observation(.1,**changes)],.1,1)
    assert t.update([observation(.2,**changes)],.2,1)


def test_identity_conflict_jump_and_stale_samples_cannot_shortcut_confirmation():
    t=TemporalConfirmation(policy="adaptive")
    t.update([observation(0)],0,1)
    assert not t.update([observation(.1,champion="Karma")],.1,1)
    assert not t.update([observation(.2,position=(.8,.8))],.2,1)
    assert not t.update([observation(.2)],.3,1)  # Not a current sample.
    assert not t.update([observation(.9)],.9,1)
    with pytest.raises(ValueError):TemporalConfirmation(policy="guess")


def test_visual_cancellation_reaches_existing_tts_without_a_second_audio_system(monkeypatch):
    from core.context import Context
    from core.cancellation import OperationCancelled
    from gui.workers import AdviceWorker
    monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED",True)
    context=Context();event=Event();stages=[]
    item=LeagueAnnouncement("Warwick visible.","visual:test",time.monotonic(),10,cancel_event=event)
    monkeypatch.setattr(context.league,"announcement_current",lambda a:True)
    monkeypatch.setattr(context.league,"announcement_status",lambda a,s:stages.append(s))
    def speak(text,**options):
        event.set()
        assert options["cancel_event"].is_set()
        with pytest.raises(OperationCancelled):
            from core.cancellation import check_cancelled
            check_cancelled(options["cancel_event"])
    monkeypatch.setattr("voice.tts.speak",speak)
    AdviceWorker([],context,item).run()
    assert stages==["dropped"]


def test_known_voice_cooldown_rejects_an_alert_that_would_expire_before_start(profile):
    clock=[100.];d=VisualAlertDispatcher(profile)
    policy=LeagueVoicePolicy(clock=lambda:clock[0],on_status=lambda a,s:d.lifecycle(a,s,clock[0]))
    policy.started(LeagueAnnouncement("previous","api",100,10))
    ann=d.update([observation()],1,now=100)[0]
    assert not policy.offer(ann) and d.retry_at["Warwick"]==105 and d.cooldowns=={}
    for step in range(1,50):
        at=100+step*.1
        assert not d.update([observation(at)],1,now=at)
    assert d.update([observation(105)],1,now=105)


def test_cancelled_visual_synthesis_does_not_publish_a_ghost_spoken_message(monkeypatch):
    from core.context import Context
    from gui.workers import AdviceWorker
    context=Context();monkeypatch.setattr("config.LEAGUE_PROACTIVE_VOICE_ENABLED",True)
    monkeypatch.setattr(context.league,"announcement_current",lambda a:True)
    item=LeagueAnnouncement("Warwick visible.","visual:test",time.monotonic(),10)
    ready=[];started=[]
    worker=AdviceWorker([],context,item);worker.advice_ready.connect(ready.append);worker.advice_started.connect(started.append)
    def speak(text,**options):
        assert ready==[] and started==[]
        options["before_playback"]()
        options["on_playback_start"]()
        assert ready==[item] and started==[item]
    monkeypatch.setattr("voice.tts.speak",speak);worker.run()


def test_visual_stage_timing_uses_numeric_monotonic_marks_only():
    from core.latency import LatencyTrace,VoiceMetrics
    m=VoiceMetrics(True);trace=LatencyTrace(m)
    for name,ns in [("visual_first_raw",1_000_000_000),("visual_confirmed",1_200_000_000),
                    ("visual_proposed",1_201_000_000),("visual_accepted",1_203_000_000),
                    ("tts_submitted",1_205_000_000),("audio_ready",1_505_000_000),("device_playback",1_535_000_000)]:
        trace.mark(name,ns)
    report=m.report()
    assert report["visual_raw_to_confirmation_runtime"]["p50_ms"]==200
    assert report["visual_proposal_to_voice_acceptance"]["p50_ms"]==2
    assert report["visual_acceptance_to_tts_dispatch"]["p50_ms"]==2
    assert report["visual_proposal_to_first_audio_ready"]["p50_ms"]==304
    assert report["visual_proposal_to_dac_estimate"]["p50_ms"]==334


def test_baseline_benchmark_requires_a_pinned_commit_before_running_git(monkeypatch):
    from benchmark_league import baseline_module
    monkeypatch.setattr("subprocess.check_output",lambda *a,**k:pytest.fail("Unvalidated revision"))
    with pytest.raises(ValueError):baseline_module("HEAD; anything","league_visual")


def test_late_cancelled_notice_cannot_cancel_a_newer_gui_notification(qapp,monkeypatch):
    from tests.test_gui import make_window
    window=make_window(qapp)
    original_cancel=window._cancel_advice
    try:
        current=LeagueAnnouncement("newer","api:new",time.monotonic(),10)
        window._pending_advice=current
        monkeypatch.setattr(window,"_cancel_advice",lambda:pytest.fail("Old callback cancelled newer work"))
        window.handle_advice_delivery(LeagueAnnouncement("old","visual:old",time.monotonic()-1,1))
        assert window._pending_advice==current
    finally:
        monkeypatch.setattr(window,"_cancel_advice",original_cancel)
        window.close()

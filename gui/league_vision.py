"""Optional local CV worker; shared capture/notification infrastructure, no LLM."""
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Event, RLock

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt

import config
from core.live_screen import FrameHistory, LiveScreenStream
from core.inference_priority import priority as inference_priority
from core.league_visual_modes import VisionMode,configured_mode,speech_permission,prepare_live_profile,roster_key


def live_allowed(league,mode=None):
    mode=configured_mode() if mode is None else VisionMode(mode)
    if mode not in (VisionMode.LIVE_OBSERVATION,VisionMode.APPROVED_ALERTS):return False
    state=league.visual_context()
    return bool(league.session_active and state and state.supported)


class LeagueVisualWorker(QThread):
    status_changed=Signal(str)
    notification=Signal(object)

    def __init__(self,league,profile_path,mode=VisionMode.LIVE_OBSERVATION,replay=None):
        super().__init__()
        self.league,self.path=league,profile_path
        self.mode=VisionMode(mode);self.replay=replay
        self.stop_event=Event()
        self.lock=RLock()
        self.dispatcher=None
        self.stream=None
        self.capture=None;self.replay_valid=False;self.deadline=0
        self.epoch=(getattr(getattr(league,"tracker",None),"epoch",0) if replay
                    else getattr(league.visual_context(),"epoch",None))
        self.stats={"analyzed_frames":0,"skipped_frames":0,"stale_frames":0,"status":"starting",
                    "confirmed_observations":0,"confirmed_sightings":0,"calibration_valid":False,
                    "mode":self.mode.value,"match_epoch":self.epoch}
        self.recent=deque(maxlen=64);self.seen=set()
        self.retain_frame=False;self.last_frame=None;self.last_frame_observations=()
        from core.league_metrics import LeagueMetrics
        self.metrics=LeagueMetrics(True)

    def stop(self):
        self.stop_event.set()
        self.invalidate("stopping")
        if self.stream:self.stream.stop()

    def invalidate(self,message="capture paused"):
        with self.lock:
            if self.dispatcher:self.dispatcher.invalidate()
            self.stats["status"]=message[:200]
            if "aspect ratio" in message or "calibrat" in message.lower():self.stats["calibration_valid"]=False

    def announcement_current(self,announcement):
        if self.stop_event.is_set() or not self.can_speak():return False
        with self.lock:
            return bool(self.dispatcher and self.dispatcher.is_current(announcement))

    def announcement_status(self,announcement,state):
        with self.lock:
            if self.dispatcher:self.dispatcher.lifecycle(announcement,state)

    def snapshot(self):
        with self.lock:
            result=dict(self.stats)
        if self.stream:result["capture"]=self.stream.status()
        if self.capture and hasattr(self.capture,"status"):result["selected_window"]=self.capture.status()
        with self.lock:
            if self.dispatcher:result["notifications"]=dict(self.dispatcher.counts)
        if self.metrics.enabled:result["latency"]=self.metrics.report()
        return result

    def can_speak(self):
        if self.stop_event.is_set():return False
        if self.replay and getattr(getattr(self.league,"tracker",None),"epoch",0)!=self.epoch:return False
        return speech_permission(self.mode,replay_valid=self.replay_valid,session_active=self.league.session_active,
                                 now=time.monotonic(),deadline=self.deadline)[0]

    def capture_allowed(self):
        if self.stop_event.is_set():return False
        if self.replay:return self.can_speak()
        state=self.league.visual_context()
        return bool(live_allowed(self.league,self.mode) and state and state.epoch==self.epoch
                    and roster_key(self.league.roster())==self.expected)

    def recovered(self):
        with self.lock:self.stats["calibration_valid"]=True

    def diagnostics(self):
        with self.lock:return {"status":self.snapshot(),"recent_observations":list(self.recent)}

    def run(self):
        old_threads=None
        try:
            import cv2
            from core.league_visual import VisualCalibration,LeagueVisualPipeline,VisualAlertDispatcher
            from core.league_capture import GameWindowCapture,RecordedGameCapture
            old_threads=cv2.getNumThreads()
            cv2.setNumThreads(1)
            if self.mode==VisionMode.OFF:return
            gate=None
            if self.replay:
                if self.league.session_active or not self.replay.get("opt_in"):
                    raise ValueError("Recorded speech test requires explicit opt-in outside an active League session")
                from benchmarks.league_vision_e2e import Eligibility,verified_clip
                from core.league_replay import local_file
                video=local_file(self.replay["video"])
                profile=VisualCalibration.load(self.path)
                gate=Eligibility(video,self.path,self.replay["manifest"],assistant_reviewed=self.replay.get("assistant_reviewed",False))
                start,duration,_,_=verified_clip(gate,self.replay.get("index",0))
                gate.enable_reviewed_regions(profile)
                self.deadline=self.replay.get("deadline",time.monotonic()+min(30,max(1,float(self.replay.get("seconds",10)))))
                self.replay_valid=True
                self.capture=RecordedGameCapture(str(video),profile,start,duration,self.capture_allowed)
                state=replace(profile.offline_lane(),epoch=self.epoch)
            else:
                if not live_allowed(self.league,self.mode):return
                state=self.league.visual_context()
                self.epoch=state.epoch
                with self.lock:self.stats["status"]="preparing current champion references"
                profile=prepare_live_profile(self.path,self.league,state,self.stop_event)
                self.expected=roster_key(profile.spec["roster"])
                self.capture=GameWindowCapture(profile,self.capture_allowed)
            if not state.supported:raise ValueError("Recognition requires a supported Summoner's Rift game or Practice Tool session")
            with self.lock:self.stats.update(calibration_valid=False,role=state.role,player_champion=state.player_champion,match_epoch=self.epoch,
                game_mode=state.mode,map_number=state.map_number,role_source=state.role_source,
                calibration_status="ROI geometry checked at capture; live HUD accuracy unverified" if not self.replay else "Recorded calibration verified against source",
                roster_size=len(profile.spec.get("roster",[])),source="recorded replay" if self.replay else "live game window")
            pipeline=None if profile.spec.get("recognition_blocked_reason") else LeagueVisualPipeline(profile)
            dispatcher=VisualAlertDispatcher(profile)
            if self.replay:dispatcher.muted=False  # Explicit test; global ADVICE mute still applies downstream.
            with self.lock:self.dispatcher=dispatcher
            history=FrameHistory(max_frames=2,max_seconds=.5,max_bytes=8*1024*1024)
            self.stream=LiveScreenStream(self.capture.open,history,min(10,profile.fps,config.LEAGUE_VISION_FPS),
                                         self.invalidate,retry_seconds=.5,on_recovered=self.recovered)
            self.stream.start()
            last_sequence=0;last_epoch=None
            began=time.monotonic();last_resources=began;last_emit=0;last_count=0;last_rate=began
            mtime=Path(self.path).stat().st_mtime_ns
            try:
                import psutil
                process=psutil.Process();cpu=process.cpu_times();cpu_begin=cpu.user+cpu.system
            except ImportError:process=None
            while not self.stop_event.is_set():
                now=time.monotonic()
                if self.replay:
                    if not self.can_speak() or self.capture.done.is_set():break
                else:
                    if not self.league.session_active:break
                    state=self.league.visual_context()
                    if state is None or not state.supported:
                        self.invalidate("waiting for fresh supported match data")
                        self.stop_event.wait(.05)
                        continue
                    if state.epoch!=self.epoch or roster_key(self.league.roster())!=self.expected:
                        self.invalidate("match or roster changed; rebuilding capture")
                        break
                if now-last_resources>=1:
                    last_resources=now
                    if Path(self.path).stat().st_mtime_ns!=mtime:
                        self.invalidate("calibration changed; restarting capture");break
                    with self.lock:
                        count=self.stats["analyzed_frames"]
                        self.stats.update(detection_fps=round((count-last_count)/max(.001,now-last_rate),2),
                                          elapsed_seconds=round(now-began,2))
                        if process:
                            cpu=process.cpu_times()
                            self.stats.update(process_cpu_percent_one_core=round(100*(cpu.user+cpu.system-cpu_begin)/max(.001,now-began),2),
                                              process_rss_bytes=process.memory_info().rss)
                        last_count=count;last_rate=now
                    self.status_changed.emit(self.snapshot()["status"])
                if not inference_priority.background_ready():
                    self.invalidate("deferred for foreground work")
                    self.stop_event.wait(.05)
                    continue
                batch=history.wait_sample(last_sequence,last_epoch,max_age=config.LEAGUE_VISION_MAX_FRAME_AGE)
                if not batch.frames:
                    self.invalidate(self.stream.error or "waiting for a fresh unobscured game frame")
                    self.stop_event.wait(.05)
                    continue
                frame=batch.frames[-1]
                if frame.sequence==last_sequence and batch.epoch==last_epoch:continue
                if batch.epoch!=last_epoch:
                    if pipeline:pipeline.temporal.reset(state.epoch)
                    last_epoch=batch.epoch
                skipped=max(0,frame.sequence-last_sequence-1) if last_sequence else 0
                last_sequence=frame.sequence
                started=time.perf_counter_ns()
                if self.metrics.enabled and frame.ready_ns:
                    self.metrics.record("capture_to_frame_ready",frame.ready_ns-frame.capture_started_ns)
                    self.metrics.record("frame_ready_to_detection_start",started-frame.ready_ns)
                observations=pipeline.process(frame.regions,state,frame.captured_at) if pipeline else ()
                self.metrics.finish("visual_processing",started)
                if frame.capture_started_ns:self.metrics.finish("capture_frame_to_detection_complete",frame.capture_started_ns)
                if time.monotonic()-frame.captured_at>config.LEAGUE_VISION_MAX_FRAME_AGE:
                    self.invalidate("stale processed frame discarded")
                    with self.lock:self.stats["stale_frames"]+=1
                    continue
                with self.lock:
                    if self.retain_frame:
                        self.last_frame=frame
                        self.last_frame_observations=tuple(o.to_dict() for o in observations)
                    eligible=gate.select(observations,frame.source_video_s,"verified",profile) if gate else ()
                    proposals=dispatcher.update(eligible if self.can_speak() else (),state.epoch,live=not bool(self.replay),approved=False)
                    current_episodes={(o.champion,o.source,o.first_seen_at,last_epoch) for o in observations}
                    self.stats["confirmed_sightings"]+=len(current_episodes-self.seen)
                    self.seen=current_episodes
                    for observation in observations:
                        self.recent.append(observation.to_dict())
                    self.stats.update(analyzed_frames=self.stats["analyzed_frames"]+1,
                                      calibration_valid=True,
                                      skipped_frames=self.stats["skipped_frames"]+skipped,
                                      processing_ms=round((time.perf_counter_ns()-started)/1e6,2),
                                      confirmed_observations=self.stats["confirmed_observations"]+len(observations),
                                      observations=len(observations),status=("recorded speech test active" if self.replay
                                          else "Capture active; "+profile.spec["recognition_blocked_reason"] if not pipeline
                                          else "local detection active (silent)"))
                for announcement in proposals:
                    if self.announcement_current(announcement):
                        self.notification.emit(announcement)  # Existing Qt -> voice policy -> TTS route.
                        self.metrics.finish("visual_proposal_to_callback",announcement.queued_ns)
                if now-last_emit>=1:
                    last_emit=now;self.status_changed.emit(self.snapshot()["status"])
        except Exception as error:
            self.invalidate(str(error))
            with self.lock:self.stats.update(calibration_valid=False,error=str(error)[:240])
            self.status_changed.emit("Local League vision unavailable: " + str(error)[:180])
        finally:
            self.invalidate("stopped")
            if self.stream:
                self.stream.stop();self.stream.join()
            if old_threads is not None:cv2.setNumThreads(old_threads)


class LeagueVisualService(QObject):
    stopped=Signal()
    notice=Signal(str)
    calibration_requested=Signal(str)
    changed=Signal()
    notification=Signal(object)

    def __init__(self,league,parent=None):
        super().__init__(parent)
        self.league=league
        self.worker=None
        self.closing=False
        self.dialog=None
        self.panel=None;self.replay=None;self.requested_mode=configured_mode();self.resume_mode=VisionMode.OFF
        self.last_status={};self.last_diagnostics={};self.next_retry_at=0;self.last_notice=""
        self.restart=False
        self.league.visual_announcement_current=self.announcement_current
        self.league.visual_announcement_status=self.announcement_status
        self.calibration_requested.connect(self._open_calibration,Qt.ConnectionType.QueuedConnection)

    def status(self):
        result=self.worker.snapshot() if self.worker else dict(self.last_status)
        state=self.league.visual_context()
        result.update(mode=self.mode.value,requested_mode=self.requested_mode.value,
            speech_enabled=bool(self.worker and self.worker.can_speak()),
            speech_disabled_reason=speech_permission(self.requested_mode,replay_valid=bool(self.worker and self.worker.replay_valid),
                session_active=self.league.session_active,now=time.monotonic(),deadline=self.worker.deadline if self.worker else 0)[1],
            running=bool(self.worker and self.worker.isRunning()))
        if self.mode==VisionMode.CONTROLLED_TEST and (not config.LEAGUE_PROACTIVE_VOICE_ENABLED
                or not getattr(self.parent(),"_advice_enabled",True)):
            result.update(speech_enabled=False,speech_disabled_reason="ADVICE or League voice is off; recorded detection remains silent")
        if not self.worker:
            result["status"]="disabled" if self.mode==VisionMode.OFF else "waiting for fresh supported match/calibration"
            result["role"]=getattr(state,"role",None)
            if self.mode!=VisionMode.OFF and self.last_status.get("error"):result["status"]=self.last_status["error"]
        return result

    @property
    def mode(self):
        return VisionMode.LIVE_OBSERVATION if self.requested_mode==VisionMode.APPROVED_ALERTS else self.requested_mode

    @property
    def observation_only(self):
        return self.mode==VisionMode.LIVE_OBSERVATION

    def request_mode(self,mode):
        mode=VisionMode(mode)
        if mode==VisionMode.CONTROLLED_TEST:raise ValueError("Use an explicitly selected local recording for a controlled test")
        self.requested_mode=mode;self.replay=None
        if self.worker:self.worker.stop()
        self.next_retry_at=0;self.sync();self.changed.emit()

    def start_replay(self,video,calibration,manifest,*,opt_in=False,assistant_reviewed=False,seconds=10,index=0):
        if opt_in is not True:raise ValueError("Explicit recorded speech-test opt-in is required")
        if self.league.session_active:raise ValueError("Finish the active League session before a recorded speech test")
        if type(seconds) not in (int,float) or not 1<=seconds<=30:raise ValueError("Use a test duration of 1..30 seconds")
        if self.requested_mode!=VisionMode.CONTROLLED_TEST:self.resume_mode=self.requested_mode
        self.replay={"video":str(video),"calibration":str(calibration),"manifest":str(manifest),"opt_in":True,
                     "assistant_reviewed":assistant_reviewed is True,"seconds":seconds,"index":index,
                     "deadline":time.monotonic()+seconds}
        self.requested_mode=VisionMode.CONTROLLED_TEST
        if self.worker:self.worker.stop()
        self.next_retry_at=0;self.sync();self.changed.emit()

    @Slot()
    def refresh(self):
        self.sync()
        if self.panel and self.panel.isVisible():self.panel.refresh()
        self.changed.emit()

    def open_status(self):
        if self.closing:return
        if self.panel is None:
            from gui.league_vision_panel import LeagueVisionPanel
            self.panel=LeagueVisionPanel(self,self.parent())
        self.panel.refresh();self.panel.show();self.panel.raise_()

    @property
    def recording_worker(self):
        return self.dialog.worker if self.dialog else None

    @Slot(object)
    def sync(self,state=None):
        if self.replay and time.monotonic()>=self.replay["deadline"] and self.worker is None:
            self.replay=None;self.requested_mode=self.resume_mode
        if self.league.session_active and self.recording_worker and self.recording_worker.operation=="detect":
            self.recording_worker.stop()
        enabled=self.mode!=VisionMode.OFF and (bool(self.replay) or self.league.session_active)
        if self.closing or not enabled:
            self.restart=False
            if self.worker:self.worker.stop()
            return
        if self.worker is not None:
            current=self.league.visual_context()
            if (self.worker.replay and self.league.session_active
                    or not self.worker.replay and current and current.epoch!=self.worker.epoch):self.worker.stop()
            if self.worker.stop_event.is_set():self.restart=True
            return
        if time.monotonic()<self.next_retry_at:return
        if not self.replay and not live_allowed(self.league,self.mode):return
        path=Path(self.replay["calibration"]) if self.replay else config.LEAGUE_VISION_CALIBRATION
        self.worker=LeagueVisualWorker(self.league,path,self.mode,self.replay)
        self.worker.status_changed.connect(self._worker_status,Qt.ConnectionType.QueuedConnection)
        self.worker.notification.connect(self.notification,Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        self.worker.start(QThread.Priority.LowPriority)

    @Slot(str)
    def _worker_status(self,message):
        self.changed.emit()
        if message.startswith("Local League vision unavailable") and message!=self.last_notice:
            self.last_notice=message;self._notice(message)

    @Slot(str)
    def _notice(self,message):
        if not self.closing:self.notice.emit(message)

    @Slot()
    def _finished(self):
        worker,self.worker=self.worker,None
        if worker:
            self.last_status=worker.snapshot();self.last_diagnostics=worker.diagnostics();worker.deleteLater()
            self.next_retry_at=time.monotonic()+(2 if self.last_status.get("error") else .2)
            if worker.replay and worker.replay is self.replay and self.requested_mode==VisionMode.CONTROLLED_TEST:
                self.requested_mode=self.resume_mode;self.replay=None
        self.stopped.emit()
        current=self.league.visual_context() if not self.closing else None
        if worker and current and current.epoch!=worker.epoch:self.restart=True
        if self.restart and not self.closing:
            self.restart=False
            self.sync()
        self.changed.emit()

    def announcement_current(self,announcement):
        return bool(self.worker and self.worker.announcement_current(announcement))

    def announcement_status(self,announcement,state):
        if self.worker:self.worker.announcement_status(announcement,state)

    def open_calibration(self,video=""):
        self.calibration_requested.emit(video)
        return {"status":"queued","message":"Opening the local recorded-frame calibration and label editor."}

    @Slot(str)
    def _open_calibration(self,video):
        if self.closing:return
        try:
            from gui.league_calibration import LeagueCalibrationDialog
            if self.dialog is None:
                self.dialog=LeagueCalibrationDialog(config.LEAGUE_VISION_CALIBRATION,video,parent=self.parent(),league=self.league)
                self.dialog.saved.connect(self._reload)
                self.dialog.closed.connect(self._dialog_closed)
            self.dialog.show();self.dialog.raise_()
        except Exception as error:self.notice.emit("Cannot open calibration: "+str(error)[:180])

    @Slot()
    def _dialog_closed(self):
        dialog,self.dialog=self.dialog,None
        if dialog:dialog.deleteLater()
        self.stopped.emit()

    @Slot()
    def _reload(self):
        if self.worker:
            self.restart=True
            self.worker.stop()
        else:self.sync()

    def close(self):
        self.closing=True
        self.league.visual_announcement_current=None
        self.league.visual_announcement_status=None
        if self.worker:self.worker.stop()
        if self.dialog:self.dialog.close()
        if self.panel:self.panel.close()

"""Optional local CV worker; shared capture/notification infrastructure, no LLM."""
import time
from threading import Event, RLock

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt

import config
from core.live_screen import FrameHistory, LiveScreenStream
from core.inference_priority import priority as inference_priority


def live_allowed(league):
    if not config.LEAGUE_VISION_ENABLED or not config.LEAGUE_VISION_RIOT_APPROVED:
        return False
    state=league.visual_context()
    return bool(state and state.supported)


class LeagueVisualWorker(QThread):
    status_changed=Signal(str)

    def __init__(self,league,profile_path):
        super().__init__()
        self.league,self.path=league,profile_path
        self.stop_event=Event()
        self.lock=RLock()
        self.dispatcher=None
        self.stream=None
        self.epoch=getattr(league.visual_context(),"epoch",None)
        self.stats={"analyzed_frames":0,"skipped_frames":0,"stale_frames":0,"status":"starting"}
        from core.league_metrics import LeagueMetrics
        self.metrics=LeagueMetrics()

    def stop(self):
        self.stop_event.set()
        if self.stream:self.stream.stop()

    def invalidate(self,message="capture paused"):
        with self.lock:
            if self.dispatcher:self.dispatcher.invalidate()
            self.stats["status"]=message[:200]

    def announcement_current(self,announcement):
        if self.stop_event.is_set() or not live_allowed(self.league):return False
        with self.lock:
            return bool(self.dispatcher and self.dispatcher.is_current(announcement))

    def announcement_status(self,announcement,state):
        with self.lock:
            if self.dispatcher:self.dispatcher.lifecycle(announcement,state)

    def snapshot(self):
        with self.lock:
            result=dict(self.stats)
        if self.stream:result["capture"]=self.stream.status()
        if self.metrics.enabled:result["latency"]=self.metrics.report()
        return result

    def run(self):
        old_threads=None
        try:
            import cv2
            from core.league_visual import VisualCalibration,LeagueVisualPipeline,VisualAlertDispatcher
            from core.league_capture import GameWindowCapture
            old_threads=cv2.getNumThreads()
            cv2.setNumThreads(1)
            if not live_allowed(self.league):return
            profile=VisualCalibration.load(self.path)
            pipeline=LeagueVisualPipeline(profile)
            dispatcher=VisualAlertDispatcher(profile)
            with self.lock:self.dispatcher=dispatcher
            expected={(p["champion"],p["side"]) for p in profile.spec["roster"]}
            def allowed():
                context=self.league.visual_context()
                return (not self.stop_event.is_set() and live_allowed(self.league) and context and context.epoch==self.epoch
                        and {(p["champion"],p["side"]) for p in self.league.roster()}==expected)
            capture=GameWindowCapture(profile,allowed)
            history=FrameHistory(max_frames=2,max_seconds=.5,max_bytes=8*1024*1024)
            self.stream=LiveScreenStream(capture.open,history,min(profile.fps,config.LEAGUE_VISION_FPS),
                                         self.invalidate,retry_seconds=.1)
            self.stream.start()
            last_sequence=0;last_epoch=None
            while not self.stop_event.wait(.02):
                state=self.league.visual_context()
                if (not config.LEAGUE_VISION_ENABLED or not config.LEAGUE_VISION_RIOT_APPROVED
                        or not self.league.session_active):
                    break
                if state is None or not state.supported:
                    self.invalidate("waiting for fresh supported match data")
                    continue  # A transient API outage must not kill automatic recovery.
                if state.epoch!=self.epoch:
                    self.invalidate("match changed")
                    break
                if not inference_priority.background_ready():
                    self.invalidate("deferred for foreground work")
                    continue
                batch=history.sample(count=1,max_age=config.LEAGUE_VISION_MAX_FRAME_AGE)
                if not batch.frames:
                    self.invalidate("waiting for a fresh unobscured game frame")
                    continue
                frame=batch.frames[-1]
                if frame.sequence==last_sequence:continue
                if batch.epoch!=last_epoch:
                    pipeline.temporal.reset(state.epoch)
                    last_epoch=batch.epoch
                skipped=max(0,frame.sequence-last_sequence-1) if last_sequence else 0
                last_sequence=frame.sequence
                started=time.perf_counter_ns()
                if self.metrics.enabled and frame.ready_ns:
                    self.metrics.record("capture_to_frame_ready",frame.ready_ns-frame.capture_started_ns)
                    self.metrics.record("frame_ready_to_detection_start",started-frame.ready_ns)
                observations=pipeline.process(frame.regions,state,frame.captured_at)
                self.metrics.finish("visual_processing",started)
                if frame.capture_started_ns:self.metrics.finish("capture_frame_to_detection_complete",frame.capture_started_ns)
                if time.monotonic()-frame.captured_at>config.LEAGUE_VISION_MAX_FRAME_AGE:
                    self.invalidate("stale processed frame discarded")
                    with self.lock:self.stats["stale_frames"]+=1
                    continue
                with self.lock:
                    proposals=dispatcher.update(observations,state.epoch,live=True,approved=config.LEAGUE_VISION_RIOT_APPROVED)
                    self.stats.update(analyzed_frames=self.stats["analyzed_frames"]+1,
                                      skipped_frames=self.stats["skipped_frames"]+skipped,
                                      processing_ms=round((time.perf_counter_ns()-started)/1e6,2),
                                      observations=len(observations),status="local detection active")
                for announcement in proposals:
                    if self.announcement_current(announcement) and self.league.on_notification:
                        self.league.on_notification(announcement)  # Existing Qt -> voice policy -> TTS route.
                        self.metrics.finish("visual_proposal_to_callback",announcement.queued_ns)
        except Exception as error:
            self.invalidate(str(error))
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

    def __init__(self,league,parent=None):
        super().__init__(parent)
        self.league=league
        self.worker=None
        self.closing=False
        self.dialog=None
        self.restart=False
        self.league.visual_announcement_current=self.announcement_current
        self.league.visual_announcement_status=self.announcement_status
        self.calibration_requested.connect(self._open_calibration,Qt.ConnectionType.QueuedConnection)

    def status(self):
        if self.worker:return self.worker.snapshot()
        return {"status":"disabled" if not config.LEAGUE_VISION_ENABLED else "approval required" if not config.LEAGUE_VISION_RIOT_APPROVED else "waiting for supported match/calibration"}

    @property
    def recording_worker(self):
        return self.dialog.worker if self.dialog else None

    @Slot(object)
    def sync(self,state=None):
        if self.league.session_active and self.recording_worker and self.recording_worker.operation=="detect":
            self.recording_worker.stop()
        enabled=config.LEAGUE_VISION_ENABLED and config.LEAGUE_VISION_RIOT_APPROVED and self.league.session_active
        if self.closing or not enabled:
            self.restart=False
            if self.worker:self.worker.stop()
            return
        if self.worker is not None:
            current=self.league.visual_context()
            if current and current.epoch!=self.worker.epoch:self.worker.stop()
            if self.worker.stop_event.is_set():self.restart=True
            return
        if not live_allowed(self.league):return
        if not config.LEAGUE_VISION_CALIBRATION.is_file():return
        self.worker=LeagueVisualWorker(self.league,config.LEAGUE_VISION_CALIBRATION)
        self.worker.status_changed.connect(self._notice,Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        self.worker.start(QThread.Priority.LowPriority)

    @Slot(str)
    def _notice(self,message):
        if not self.closing:self.notice.emit(message)

    @Slot()
    def _finished(self):
        worker,self.worker=self.worker,None
        if worker:worker.deleteLater()
        self.stopped.emit()
        current=self.league.visual_context() if not self.closing else None
        if worker and current and current.epoch!=worker.epoch:self.restart=True
        if self.restart and not self.closing:
            self.restart=False
            self.sync()

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

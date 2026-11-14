"""One explicitly requested offline job. Qt owns lifecycle; no polling timer."""
import uuid
from threading import Event, Lock

from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt

import config
from core.cancellation import OperationCancelled, check_cancelled
from core.inference_priority import priority as inference_priority


class ReplayWorker(QThread):
    result = Signal(object)
    failure = Signal(str)

    def __init__(self, league, video, calibration, output, cancellation):
        super().__init__()
        self.league, self.video, self.calibration = league, video, calibration
        self.output, self.cancellation = output, cancellation

    def stop(self):
        self.cancellation.set()

    def run(self):
        def admit():
            while True:
                check_cancelled(self.cancellation)
                if self.league.session_active:
                    raise OperationCancelled()
                if inference_priority.background_ready():
                    return
                self.cancellation.wait(.05)
        try:
            admit()
            from core.league_replay import analyze_video
            report = analyze_video(self.video,self.calibration,self.output,self.cancellation,admit)
            check_cancelled(self.cancellation)
            self.result.emit(report)
        except OperationCancelled:
            pass
        except ImportError:
            self.failure.emit("Install the optional replay requirements before recorded-game analysis.")
        except Exception as error:
            self.failure.emit(str(error)[:400])


class ReplayService(QObject):
    requested = Signal(object)
    stopped = Signal()
    notice = Signal(str)

    def __init__(self, league, parent=None):
        super().__init__(parent)
        self.league = league
        self.worker = None
        self._lock = Lock()
        self._busy, self._closing = False, False
        self._cancel = Event()
        self._status = {"status":"idle"}
        self.requested.connect(self._start,Qt.ConnectionType.QueuedConnection)

    def submit(self, video, calibration):
        from core.league_replay import local_file
        if not config.LEAGUE_REPLAY_ENABLED:
            return {"status":"disabled","message":"Recorded-game analysis is disabled."}
        if self.league.session_active:
            return {"status":"unavailable","message":"Recorded-game analysis is available after the active League session ends."}
        video, calibration = local_file(video), local_file(calibration)
        with self._lock:
            if self._busy or self._closing:
                return {"status":"busy","message":"An analysis is already pending or stopping."}
            self._busy, self._cancel = True, Event()
            job = uuid.uuid4().hex
            output = config.DATA_DIR / "league-replays" / job
            self._status = {"status":"queued","job_id":job,"report_path":str(output/"report.json")}
            result = dict(self._status)
        self.league.journal.publish_replay(None)
        self.requested.emit((video,calibration,output))
        return result

    @Slot(object)
    def _start(self, job):
        if self._closing or self._cancel.is_set() or self.league.session_active:
            with self._lock:
                self._busy = False
                self._status = {"status":"cancelled"}
            self.stopped.emit()
            return
        self.worker = ReplayWorker(self.league,*job,self._cancel)
        self.worker.result.connect(self._result,Qt.ConnectionType.QueuedConnection)
        self.worker.failure.connect(self._failure,Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._finished,Qt.ConnectionType.QueuedConnection)
        with self._lock:
            self._status["status"] = "running"
        self.worker.start(QThread.Priority.LowPriority)

    @Slot(object)
    def _result(self, report):
        if self._closing or self._cancel.is_set() or self.league.session_active:
            return
        self.league.journal.publish_replay(report)
        with self._lock:
            self._status.update(status="complete",candidates=len(report["candidates"]))
        self.notice.emit("Recorded-game analysis is ready. Review the candidate thumbnails before confirming sightings.")

    @Slot(str)
    def _failure(self, message):
        with self._lock:
            self._status.update(status="error",message=message)
        if not self._closing:
            self.notice.emit("Recorded-game analysis could not finish: " + message)

    @Slot()
    def _finished(self):
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        with self._lock:
            self._busy = False
            if self._cancel.is_set() or self._status.get("status") == "running":
                self._status["status"] = "cancelled"
        self.stopped.emit()

    def status(self):
        with self._lock:
            return dict(self._status)

    def cancel(self, active=True):
        if active:
            self._cancel.set()

    def close(self):
        self._closing = True
        self.cancel()

    def load_reviewed(self, path):
        from core.league_replay import load_report
        if self.league.session_active:
            return {"status":"unavailable","message":"Load post-game analysis after the active session."}
        with self._lock:
            if self._busy:
                return {"status":"busy","message":"Wait for the current recording job before loading a different report."}
        report = load_report(path)
        self.league.journal.publish_replay(report)
        return {"status":"loaded","confirmed_sightings":len(report["confirmed_sightings"])}

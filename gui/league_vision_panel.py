"""On-demand aggregate diagnostics; competitive observations are never tactical UI."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,
                              QFileDialog,QCheckBox,QFormLayout,QSpinBox)
import config
from core.league_visual_modes import VisionMode


class LeagueVisionPanel(QDialog):
    def __init__(self,service,parent=None):
        super().__init__(parent);self.service=service
        self.setWindowTitle("Capri — League visual observation");self.resize(600,500)
        layout=QVBoxLayout(self)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        row=QHBoxLayout()
        for text,mode in (("Start observation",VisionMode.LIVE_OBSERVATION),("Stop",VisionMode.OFF),
                          ("Request approved alerts",VisionMode.APPROVED_ALERTS)):
            button=QPushButton(text);button.clicked.connect(lambda checked=False,m=mode:service.request_mode(m));row.addWidget(button)
        layout.addLayout(row)
        button=QPushButton("Open calibration editor");button.clicked.connect(lambda:service.open_calibration());layout.addWidget(button)
        layout.addWidget(QLabel("Short recorded speech test — finish any active League session first."))
        form=QFormLayout();root=config.DATA_DIR/"league-vision"
        self.video=QLineEdit(str(Path("tests/fixtures/league_real_gameplay.mp4").resolve()))
        self.calibration=QLineEdit(str(root/"e2e-eligibility-calibration.json"))
        self.manifest=QLineEdit(str(root/"e2e-eligibility.json"))
        for name,field in (("Local recording",self.video),("Replay calibration",self.calibration),("Reviewed whitelist",self.manifest)):
            row=QHBoxLayout();row.addWidget(field);button=QPushButton("Browse")
            button.clicked.connect(lambda checked=False,f=field:self.browse(f));row.addWidget(button);form.addRow(name,row)
        self.duration=QSpinBox();self.duration.setRange(1,30);self.duration.setValue(10);form.addRow("Maximum seconds",self.duration)
        self.assistant=QCheckBox("Explicitly use assistant-reviewed replay evidence (not human validation)")
        self.opt_in=QCheckBox("Play a short spoken test from this local recording")
        form.addRow(self.assistant);form.addRow(self.opt_in);layout.addLayout(form)
        button=QPushButton("Start recorded speech test");button.clicked.connect(self.start_test);layout.addWidget(button)
        self.message=QLabel();self.message.setWordWrap(True);layout.addWidget(self.message)
        layout.addWidget(QLabel("Live observation is silent. Local configuration is not proof of Riot approval.\n"
                               "No frames are saved. Counts and timings do not establish recognition accuracy."))

    def browse(self,field):
        path,_=QFileDialog.getOpenFileName(self,"Select a local replay file",field.text())
        if path:field.setText(path)

    def start_test(self):
        try:
            self.service.start_replay(self.video.text(),self.calibration.text(),self.manifest.text(),
                opt_in=self.opt_in.isChecked(),assistant_reviewed=self.assistant.isChecked(),seconds=self.duration.value())
            self.message.setText("Recorded test requested. ADVICE and game voice settings still control speech.")
        except ValueError as error:self.message.setText(str(error))
        self.refresh()

    def refresh(self):
        s=self.service.status();capture=s.get("capture",{});window=s.get("selected_window",{})
        self.status.setText(
            f"Mode: {s['mode']} (requested: {s['requested_mode']})\n"
            f"Capture: {s.get('status')}\nWindow: {window.get('title',window.get('application','not selected'))} "
            f"{window.get('client_physical','')}\nMatch epoch: {s.get('match_epoch','unknown')}; mode: {s.get('game_mode','unknown')}; role: {s.get('role') or 'unknown'}\n"
            f"Roster: {s.get('roster_size',0)} current API champions\n"
            f"Capture: {capture.get('capture_fps',0)} FPS; detection: {s.get('detection_fps',0)} FPS\n"
            f"Confirmed sighting episodes: {s.get('confirmed_sightings',0)}; analyzed frames: {s.get('analyzed_frames',0)}\n"
            f"Skipped: {s.get('skipped_frames',0)}; stale: {s.get('stale_frames',0)}\n"
            f"Calibration geometry valid: {s.get('calibration_valid',False)}; {s.get('calibration_status','awaiting capture')}\n"
            f"Neko CPU (one core): {s.get('process_cpu_percent_one_core',0)}%; RSS: {s.get('process_rss_bytes',0)/1048576:.1f} MiB\n"
            f"Speech: {s['speech_disabled_reason']}")

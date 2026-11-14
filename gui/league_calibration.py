"""On-demand local recording calibration and human review; no continuous recording."""
import copy
import json
import time
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QRectF, QThread, Signal, Slot
from PySide6.QtGui import QImage, QPainter, QColor, QPen
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,
    QPushButton,QFileDialog,QDoubleSpinBox,QComboBox,QCheckBox,QPlainTextEdit,QTabWidget,
    QTableWidget,QTableWidgetItem,QAbstractItemView,QWidget,QMessageBox,QSplitter)

from core.inference_priority import priority as inference_priority
from core.league_visual_evaluation import new_labels,validate_labels,save_json
from core.league_replay import local_file,read_json


class FrameCanvas(QWidget):
    rectangle_selected=Signal(object)
    point_selected=Signal(object)

    def __init__(self):
        super().__init__()
        self.image=None;self.box=None;self.detections=[];self.start=None;self.end=None
        self.setMinimumSize(420,260)

    def image_rect(self):
        if self.image is None:return QRectF()
        ratio=min(self.width()/self.image.width(),self.height()/self.image.height())
        w,h=self.image.width()*ratio,self.image.height()*ratio
        return QRectF((self.width()-w)/2,(self.height()-h)/2,w,h)

    def normalized(self,point):
        rect=self.image_rect()
        if rect.isEmpty() or not rect.contains(point):return None
        return ((point.x()-rect.x())/rect.width(),(point.y()-rect.y())/rect.height())

    def set_image(self,image):
        self.image=image;self.start=self.end=None;self.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),QColor("#151218"))
        rect=self.image_rect()
        if self.image is None:
            painter.setPen(QColor("#eeeeee"));painter.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,"Load a local recorded frame. Drag a rectangle to calibrate.")
            return
        painter.drawImage(rect,self.image)
        def draw(box,color):
            x,y,w,h=box;painter.setPen(QPen(QColor(color),2))
            painter.drawRect(QRectF(rect.x()+x*rect.width(),rect.y()+y*rect.height(),w*rect.width(),h*rect.height()))
        if self.box:draw(self.box,"#00eeaa")
        for box,label in self.detections:
            draw(box,"#ff0033")
            painter.drawText(int(rect.x()+box[0]*rect.width()),max(12,int(rect.y()+box[1]*rect.height()-4)),label)
        if self.start and self.end:
            x,y=self.start;a,b=self.end;draw((min(x,a),min(y,b),abs(a-x),abs(b-y)),"#ffbb33")

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.start=self.normalized(event.position());self.end=self.start

    def mouseMoveEvent(self,event):
        if self.start:
            self.end=self.normalized(event.position()) or self.end;self.update()

    def mouseReleaseEvent(self,event):
        if not self.start:return
        end=self.normalized(event.position()) or self.start
        x,y=self.start;a,b=end
        self.start=self.end=None
        if abs(x-a)<.005 and abs(y-b)<.005:self.point_selected.emit(end)
        else:self.rectangle_selected.emit([min(x,a),min(y,b),abs(x-a),abs(y-b)])
        self.update()


class RecordingTask(QThread):
    result=Signal(object)
    failed=Signal(str)

    def __init__(self,operation,arguments,league=None):
        super().__init__();self.operation=operation;self.arguments=arguments;self.cancel=Event();self.league=league

    def stop(self):self.cancel.set()

    def run(self):
        try:
            if self.operation=="icons":
                from core.league_icons import prepare_icons
                result=prepare_icons(*self.arguments,cancellation=self.cancel,
                                     knowledge=getattr(self.league,"knowledge",None))
                self.result.emit({"operation":"icons","result":result});return
            if self.operation=="evaluate":
                from core.league_visual_evaluation import evaluate_labels
                report=evaluate_labels(*self.arguments)
                self.result.emit({"operation":"evaluate","report":report})
                return
            if self.operation=="detect":
                from core.league_visual_evaluation import analyze_recording
                from core.cancellation import check_cancelled
                def admit():
                    while True:
                        if self.league is not None and self.league.session_active:self.cancel.set()
                        check_cancelled(self.cancel)
                        if inference_priority.background_ready():return
                        self.cancel.wait(.05)
                report=analyze_recording(*self.arguments,cancellation=self.cancel,admit=admit,
                                         knowledge=getattr(self.league,"knowledge",None))
                self.result.emit({"operation":"detect","path":str(Path(self.arguments[2])/"detections.json"),"report":report})
                return
            import cv2
            from core.league_visual_evaluation import open_recording
            from core.league_replay import finite
            video,at,*indices=self.arguments
            cap=open_recording(video)
            try:
                fps,count=cap.get(cv2.CAP_PROP_FPS),cap.get(cv2.CAP_PROP_FRAME_COUNT)
                if not cap.isOpened() or not finite(fps) or fps<=0 or not finite(count) or not 0<count/fps<=14400:
                    raise ValueError("Use a local recording up to four hours")
                if indices:cap.set(cv2.CAP_PROP_POS_FRAMES,indices[0])
                else:cap.set(cv2.CAP_PROP_POS_MSEC,at*1000)
                ok,frame=cap.read()
                if not ok or max(frame.shape[:2])>7680:raise ValueError("Cannot read that recorded frame")
                frame=frame.copy()
                image=QImage(frame.data,frame.shape[1],frame.shape[0],frame.strides[0],QImage.Format.Format_BGR888).copy()
                if not self.cancel.is_set():
                    pts=cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                    self.result.emit({"operation":"frame","frame":frame,"image":image,"duration":count/fps,
                                      "video":str(local_file(video)),
                                      "at":pts if finite(pts) and pts>=0 else max(0,cap.get(cv2.CAP_PROP_POS_FRAMES)-1)/fps})
            finally:cap.release()
        except Exception as error:
            if not self.cancel.is_set():self.failed.emit(str(error)[:400])


class LeagueCalibrationDialog(QDialog):
    saved=Signal()
    closed=Signal()

    def __init__(self,calibration,video="",parent=None,*,detections="",labels="",league=None):
        super().__init__(parent)
        self.setWindowTitle("Capri — recorded League calibration and sighting review")
        self.resize(1120,850)
        self.path=Path(calibration).expanduser().resolve();self.league=league
        self.worker=None;self.closing=False;self.frame=None;self.loaded_at=None;self.loaded_video=None;self.report=None;self.candidates=[]
        self.label_path=Path(labels).resolve() if labels else self.path.parent/"labels.json"
        self.manifest=new_labels(video,self.path)
        self.spec=read_json(self.path,65536) if self.path.is_file() else {
            "reference_size":[1920,1080],"minimap":[.84,.72,.15,.27],"perspective":"player","fog_of_war":"player",
            "game_mode":"CLASSIC","map_number":11,"assigned_role":"UNKNOWN","fallback_role":"UNKNOWN",
            "player_champion":"","team_orientation":"unknown","fps":10,"threshold":.92,"margin":.08,
            "hud_scale":1.,"proximity_radius":.12,"alert_cooldown":8.,"alerts_muted":True,
            "roster":[],"templates":{},"regions":[],"hud_exclusions":[],"blockers":[]}
        root=QVBoxLayout(self)
        root.addWidget(QLabel("Offline player-view review. Similarity scores are not probabilities. Live competitive alerts remain disabled pending Riot approval."))
        row=QHBoxLayout();self.video=QLineEdit(video);row.addWidget(self.video)
        self.add_button(row,"Browse video",self.browse_video);root.addLayout(row)
        row=QHBoxLayout();self.at=QDoubleSpinBox();self.at.setRange(0,14400);self.at.setDecimals(3);self.at.setSuffix(" s")
        row.addWidget(self.at);self.add_button(row,"Load frame",self.load_frame)
        self.mode=QComboBox();self.mode.addItems(["Minimap ROI","Viewport ROI","Lane region","HUD exclusion","Champion minimap icon","Champion nameplate","Scoreboard blocker"])
        row.addWidget(self.mode);self.name=QLineEdit();self.name.setPlaceholderText("Champion or region name (exact roster spelling)");row.addWidget(self.name)
        self.region_lane=QComboBox();self.region_lane.addItems(["No lane","TOP","MIDDLE","BOTTOM"]);row.addWidget(self.region_lane)
        root.addLayout(row)
        splitter=QSplitter(Qt.Orientation.Vertical);self.canvas=FrameCanvas();splitter.addWidget(self.canvas)
        tabs=QTabWidget();splitter.addWidget(tabs);root.addWidget(splitter,1)
        self.canvas.rectangle_selected.connect(self.rectangle_selected);self.canvas.point_selected.connect(self.point_selected)
        setup=QWidget();form=QVBoxLayout(setup)
        form.addWidget(QLabel("Enter the known ally/enemy roster. Missing champion icons are fetched from Riot and cached automatically when analysis starts.\nManual icon cropping is optional. Viewport matching separately needs nameplate references and a scoreboard blocker."))
        self.editor=QPlainTextEdit(json.dumps(self.spec,indent=2));form.addWidget(self.editor)
        row=QHBoxLayout();self.add_button(row,"Save calibration",self.save_calibration)
        self.add_button(row,"Get champion icons",self.get_icons)
        self.add_button(row,"Generate candidates",self.generate);self.add_button(row,"Open detections",self.browse_detections);form.addLayout(row)
        self.muted=QCheckBox("Mute local visual alerts (live capture still requires Riot approval)")
        self.muted.setChecked(bool(self.spec.get("alerts_muted",True)));self.muted.toggled.connect(self.mute_changed);form.addWidget(self.muted)
        tabs.addTab(setup,"Calibration JSON / frame selection")
        review=QWidget();layout=QVBoxLayout(review)
        self.table=QTableWidget(0,6);self.table.setHorizontalHeaderLabels(["ID","Time","Champion","Source","Score","Decision"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.itemSelectionChanged.connect(self.select_candidate)
        layout.addWidget(self.table)
        row=QHBoxLayout();self.add_button(row,"Confirm selected",self.confirm);self.add_button(row,"Reject selected",self.reject)
        self.add_button(row,"Open labels",self.open_labels);self.add_button(row,"Save labels JSON",self.save_labels);layout.addLayout(row)
        tabs.addTab(review,"Confirm / reject candidates")
        truth=QWidget();layout=QVBoxLayout(truth)
        layout.addWidget(QLabel("For recall, add missed visible sightings too. Label exact sampled frames or genuinely continuous visible intervals.\nDo not fill fog-of-war gaps. A candidate confirmation adds only its inspected confirmation frame. Click an icon to set its center."))
        fields=QFormLayout();self.label_champ=QLineEdit();fields.addRow("Champion",self.label_champ)
        self.source=QComboBox();self.source.addItems(["minimap","viewport"]);fields.addRow("Source",self.source)
        self.start=QDoubleSpinBox();self.end=QDoubleSpinBox()
        for box in (self.start,self.end):box.setRange(0,14400);box.setDecimals(3)
        row=QHBoxLayout();row.addWidget(self.start);row.addWidget(self.end);fields.addRow("Visible start / end seconds",row)
        self.x=QDoubleSpinBox();self.y=QDoubleSpinBox()
        for box in (self.x,self.y):box.setRange(0,1);box.setDecimals(5)
        row=QHBoxLayout();row.addWidget(self.x);row.addWidget(self.y);fields.addRow("Center inside selected source ROI",row)
        self.near=QComboBox();self.near.addItems(["unknown","yes","no"]);fields.addRow("Near visible player (manually verified)",self.near)
        self.onset=QCheckBox("First appearance time verified against preceding absence")
        fields.addRow(self.onset)
        self.kind=QComboBox();self.kind.addItems(["unverified","real_player_view","synthetic"]);fields.addRow("Recording kind",self.kind)
        self.cover_start=QDoubleSpinBox();self.cover_end=QDoubleSpinBox()
        for box in (self.cover_start,self.cover_end):box.setRange(0,14400);box.setDecimals(3)
        row=QHBoxLayout();row.addWidget(self.cover_start);row.addWidget(self.cover_end);fields.addRow("Reviewed coverage start / end",row)
        self.complete=QCheckBox("All visible sightings AND absences in coverage are labelled (including missed detections)")
        self.complete_sources=QComboBox();self.complete_sources.addItems(["minimap","viewport","minimap + viewport"])
        fields.addRow("Exhaustively labelled sources",self.complete_sources);fields.addRow(self.complete)
        layout.addLayout(fields)
        row=QHBoxLayout();self.add_button(row,"Add missed / visible sighting",self.add_label);self.add_button(row,"Remove selected label",self.remove_label);layout.addLayout(row)
        self.truth_table=QTableWidget(0,5);self.truth_table.setHorizontalHeaderLabels(["Start","End","Champion","Source","Position"])
        self.truth_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.truth_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.truth_table)
        row=QHBoxLayout();self.add_button(row,"Save labels JSON",self.save_labels);self.add_button(row,"Generate accuracy report",self.evaluate);layout.addLayout(row)
        tabs.addTab(truth,"Ground truth / missed sightings")
        self.message=QLabel("No recording loaded. Templates and frames stay local.");self.message.setWordWrap(True);root.addWidget(self.message)
        if detections:self.load_detections(detections)
        if labels and Path(labels).is_file():self.load_labels(labels)

    @staticmethod
    def add_button(layout,text,slot):
        button=QPushButton(text);button.clicked.connect(slot);layout.addWidget(button);return button

    def error(self,error):self.message.setText(str(error))

    def read_spec(self):
        result=json.loads(self.editor.toPlainText())
        if not isinstance(result,dict):raise ValueError("Calibration must be an object")
        self.spec=result;return result

    def browse_video(self):
        path,_=QFileDialog.getOpenFileName(self,"Local player-view recording",self.video.text(),"Video (*.mp4 *.avi *.mkv *.mov)")
        if path:self.video.setText(path)

    def launch(self,operation,arguments):
        if self.worker is not None:self.error("A recording task is already running.");return
        self.worker=RecordingTask(operation,arguments,self.league)
        if operation=="icons":self.editor.setReadOnly(True)
        self.worker.result.connect(self.task_result,Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self.error,Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.task_finished,Qt.ConnectionType.QueuedConnection)
        self.worker.start(QThread.Priority.LowPriority)
        self.message.setText("Reading local recording…" if operation=="frame" else
                             "Preparing cached official champion icons…" if operation=="icons" else
                             "Analyzing locally; foreground conversation has priority…")

    @Slot()
    def load_frame(self):self.launch("frame",(self.video.text(),self.at.value()))

    @Slot(object)
    def task_result(self,result):
        if self.closing:return
        if result["operation"]=="icons":
            self.spec=read_json(self.path,65536);self.editor.setPlainText(json.dumps(self.spec,indent=2))
            if result["result"].get("prepared"):self.saved.emit()
            self.message.setText("Champion icons ready. You can generate candidates without manual cropping or confirming each detection.")
        elif result["operation"]=="evaluate":
            path=self.label_path.with_name(self.label_path.stem+"-accuracy.json");save_json(path,result["report"])
            self.message.setText(f"Saved accuracy/latency report: {path}. {result['report']['status']}")
        elif result["operation"]=="detect":
            self.load_detections(result["path"]);self.message.setText(f'Analysis complete: {result["path"]}. Review is optional for use; exhaustive labels are needed only to measure accuracy.')
        else:
            self.frame=result["frame"];self.canvas.set_image(result["image"])
            self.loaded_at=result["at"]
            self.loaded_video=result["video"]
            self.at.setValue(result["at"]);self.cover_end.setValue(result["duration"])
            self.start.setValue(result["at"]);self.end.setValue(result["at"])
            self.message.setText(f'Loaded frame at {result["at"]:.3f}s. Drag the selected ROI or reference; click an icon to label its center.')

    @Slot()
    def task_finished(self):
        worker,self.worker=self.worker,None
        self.editor.setReadOnly(False)
        if worker:worker.deleteLater()
        if self.closing:self.close()

    def mute_changed(self,muted):
        try:
            spec=self.read_spec();spec["alerts_muted"]=muted
            self.editor.setPlainText(json.dumps(spec,indent=2))
        except Exception as error:self.error(error)

    @Slot(object)
    def rectangle_selected(self,box):
        if self.worker is not None and self.worker.operation=="icons":
            self.error("Wait for icon preparation before changing calibration");return
        try:
            spec=self.read_spec();mode=self.mode.currentText();name=self.name.text().strip()
            if self.frame is None:raise ValueError("Load a frame first")
            h,w=self.frame.shape[:2]
            spec["reference_size"]=[w,h]
            if mode=="Minimap ROI":spec["minimap"]=box
            elif mode=="Viewport ROI":spec["viewport"]=box
            elif mode=="HUD exclusion":spec.setdefault("hud_exclusions",[]).append(box)
            elif mode=="Lane region":
                roi=spec["minimap"];local=[(box[0]-roi[0])/roi[2],(box[1]-roi[1])/roi[3],box[2]/roi[2],box[3]/roi[3]]
                from core.league_replay import bounds
                bounds(local)
                if not name:raise ValueError("Name the region and set its lane in JSON (TOP, MIDDLE, BOTTOM)")
                spec.setdefault("regions",[]).append({"label":name,"bounds":local,"lane":None if self.region_lane.currentIndex()==0 else self.region_lane.currentText()})
            else:
                import cv2
                crop=self.frame[round(box[1]*h):round((box[1]+box[3])*h),round(box[0]*w):round((box[0]+box[2])*w)]
                maximum=128 if mode=="Champion minimap icon" else 256 if mode=="Champion nameplate" else 512
                if min(crop.shape[:2])<6 or max(crop.shape[:2])>maximum:raise ValueError(f"Crop a tight reference, 6..{maximum}px")
                if mode!="Scoreboard blocker" and name not in {p.get("champion") for p in spec.get("roster",[])}:
                    raise ValueError("Enter the exact champion from the known roster first")
                folder=self.path.parent/"references";folder.mkdir(parents=True,exist_ok=True)
                filename=f"reference-{time.time_ns()}.png";path=folder/filename
                if not cv2.imwrite(str(path),crop):raise ValueError("Could not save reference")
                relative=str(path.relative_to(self.path.parent))
                if mode=="Champion minimap icon":spec.setdefault("templates",{})[name]=relative
                elif mode=="Champion nameplate":spec.setdefault("viewport_nameplates",{}).setdefault(name,[]).append(relative)
                else:spec.setdefault("blockers",[]).append({"region":box,"image":relative})
            self.spec=spec;self.editor.setPlainText(json.dumps(spec,indent=2));self.canvas.box=box;self.canvas.update()
            self.message.setText("Selection applied locally. Save calibration when all references and measured settings are complete.")
        except Exception as error:self.error(error)

    @Slot(object)
    def point_selected(self,point):
        try:
            roi=self.read_spec().get("minimap" if self.source.currentText()=="minimap" else "viewport")
            if roi is None:raise ValueError("Calibrate the selected source region first")
            x,y=(point[0]-roi[0])/roi[2],(point[1]-roi[1])/roi[3]
            if not 0<=x<=1 or not 0<=y<=1:raise ValueError("Click inside the selected source region")
            self.x.setValue(x);self.y.setValue(y)
        except Exception as error:self.error(error)

    @Slot()
    def save_calibration(self):
        if self.worker is not None and self.worker.operation=="icons":
            self.error("Wait for icon preparation before saving calibration");return
        try:
            from core.league_visual import VisualCalibration
            spec=self.read_spec();profile=VisualCalibration(spec,self.path.parent)
            # Decode/validate templates during the explicit background analysis, never during paint.
            profile.rois();save_json(self.path,spec);self.saved.emit()
            self.message.setText(f"Saved {self.path}. Candidate generation validates roster references before processing.")
        except Exception as error:self.error(error)

    @Slot()
    def get_icons(self):
        if self.worker is not None:self.error("A recording task is already running.");return
        try:
            spec=self.read_spec();save_json(self.path,spec)
            self.launch("icons",(self.path,))
        except Exception as error:self.error(error)

    @Slot()
    def generate(self):
        if self.worker is not None:self.error("A recording task is already running.");return
        try:
            if self.league is not None and self.league.session_active:raise ValueError("Recorded analysis is post-match; finish the active session first")
            from core.league_visual import VisualCalibration
            video=local_file(self.video.text());spec=self.read_spec()
            VisualCalibration(spec,self.path.parent).rois();save_json(self.path,spec)
            self.saved.emit()
            folder=self.path.parent/f"review-{time.time_ns()}"
            self.launch("detect",(video,self.path,folder))
        except Exception as error:self.error(error)

    def browse_detections(self):
        path,_=QFileDialog.getOpenFileName(self,"Detection candidates",str(self.path.parent),"JSON (*.json)")
        if path:
            try:self.load_detections(path)
            except Exception as error:self.error(error)

    def load_detections(self,path):
        report=read_json(path,20_000_000)
        if not isinstance(report.get("candidates"),list):raise ValueError("Not a Phase 6 detections file")
        self.path=local_file(report["calibration"]);self.spec=read_json(self.path,65536)
        self.editor.setPlainText(json.dumps(self.spec,indent=2))
        self.muted.blockSignals(True);self.muted.setChecked(self.spec.get("alerts_muted",True));self.muted.blockSignals(False)
        self.report=report;self.detections_path=Path(path).resolve();self.candidates=report["candidates"]
        self.video.setText(report["video"]);self.cover_end.setValue(report["duration_s"])
        self.refresh_candidates()

    def refresh_candidates(self):
        decisions={r["candidate_id"]:r["decision"] for r in self.manifest["reviews"]}
        self.table.blockSignals(True);self.table.setRowCount(len(self.candidates))
        for row,item in enumerate(self.candidates):
            values=[item["id"],f'{item["confirmed_at_s"]:.3f}',item["champion"],item["source"],f'{item["confidence"]:.3f}',decisions.get(item["id"],"unreviewed")]
            for col,value in enumerate(values):self.table.setItem(row,col,QTableWidgetItem(value))
        self.table.blockSignals(False)

    def select_candidate(self):
        row=self.table.currentRow()
        if not 0<=row<len(self.candidates):return
        item=self.candidates[row];self.at.setValue(item["confirmed_at_s"])
        self.label_champ.setText(item["champion"]);self.source.setCurrentText(item["source"])
        self.x.setValue(item["position"][0]);self.y.setValue(item["position"][1])
        self.start.setValue(item["confirmed_at_s"]);self.end.setValue(item["confirmed_at_s"])
        roi=self.spec.get("minimap" if item["source"]=="minimap" else "viewport")
        if roi:
            x,y,w,h=item["box"];box=[roi[0]+x*roi[2],roi[1]+y*roi[3],w*roi[2],h*roi[3]]
            self.canvas.detections=[(box,f'{item["champion"]} {item["confidence"]:.3f} t={item["confirmed_at_s"]:.2f}')]
        if "frame" in item:self.launch("frame",(self.video.text(),item["confirmed_at_s"],item["frame"]))
        else:self.load_frame()

    def decision(self,value):
        row=self.table.currentRow()
        if not 0<=row<len(self.candidates):self.error("Select a candidate first");return
        identity=self.candidates[row]["id"]
        self.manifest["reviews"]=[r for r in self.manifest["reviews"] if r["candidate_id"]!=identity]
        self.manifest["reviews"].append({"candidate_id":identity,"decision":value})
        if value=="rejected":
            self.manifest["labels"]=[item for item in self.manifest["labels"] if item.get("candidate_id")!=identity]
            self.refresh_labels()
        self.refresh_candidates()

    def confirm(self):
        if self.frame is None:self.error("Load and inspect the candidate frame first");return
        row=self.table.currentRow()
        if not 0<=row<len(self.candidates):return
        if (Path(self.video.text()).resolve()!=Path(self.report["video"]).resolve()
                or self.loaded_video!=self.report["video"]):
            self.error("Load this candidate's recording before confirming");return
        if self.loaded_at is None or abs(self.loaded_at-self.candidates[row]["confirmed_at_s"])>.002:
            self.error("Load the selected candidate's confirmation frame before confirming");return
        identity=self.candidates[row]["id"]
        self.decision("confirmed");self._append_label(identity)

    def reject(self):self.decision("rejected")

    def add_label(self):self._append_label()

    def _append_label(self,candidate_id=None):
        label={"start_s":self.start.value(),"end_s":self.end.value(),"champion":self.label_champ.text().strip(),
               "source":self.source.currentText(),"position":[self.x.value(),self.y.value()],
               "onset_known":self.onset.isChecked(),
               "near_player":{"unknown":None,"yes":True,"no":False}[self.near.currentText()]}
        if candidate_id:label["candidate_id"]=candidate_id
        if not label["champion"] or label["start_s"]>label["end_s"]:self.error("Set champion and a valid visible interval");return
        if label not in self.manifest["labels"]:self.manifest["labels"].append(label)
        self.refresh_labels();self.message.setText("Added a human label. Save labels JSON; add any missed visible sightings too.")

    def remove_label(self):
        row=self.truth_table.currentRow()
        if 0<=row<len(self.manifest["labels"]):self.manifest["labels"].pop(row);self.refresh_labels()

    def refresh_labels(self):
        self.truth_table.setRowCount(len(self.manifest["labels"]))
        for row,item in enumerate(self.manifest["labels"]):
            for col,value in enumerate((item["start_s"],item["end_s"],item["champion"],item["source"],item["position"])):
                self.truth_table.setItem(row,col,QTableWidgetItem(str(value)))

    def open_labels(self):
        path,_=QFileDialog.getOpenFileName(self,"Human labels",str(self.label_path),"JSON (*.json)")
        if path:
            try:self.load_labels(path)
            except Exception as error:self.error(error)

    def load_labels(self,path):
        value=validate_labels(read_json(path,20_000_000))
        if self.report is not None and (local_file(value["video"])!=local_file(self.report["video"])
                                        or local_file(value["calibration"])!=local_file(self.report["calibration"])):
            raise ValueError("Labels belong to another recording/calibration; open its detections first")
        self.manifest=value;self.label_path=Path(path).resolve()
        self.kind.setCurrentText(self.manifest["kind"]);self.complete.setChecked(self.manifest["annotation_complete"])
        self.complete_sources.setCurrentText(" + ".join(self.manifest["complete_for"]))
        self.cover_start.setValue(self.manifest["coverage"]["start_s"]);self.cover_end.setValue(self.manifest["coverage"]["end_s"])
        self.refresh_candidates();self.refresh_labels()

    def labels_value(self):
        value=copy.deepcopy(self.manifest)
        coverage=dict(value.get("coverage",{}))
        coverage.update(start_s=self.cover_start.value(),end_s=self.cover_end.value())
        value.update(video=str(Path(self.video.text()).resolve()),calibration=str(self.path),kind=self.kind.currentText(),
                     coverage=coverage,
                     annotation_complete=self.complete.isChecked(),complete_for=self.complete_sources.currentText().split(" + "))
        return validate_labels(value)

    def save_labels(self):
        try:
            value=self.labels_value();save_json(self.label_path,value);self.manifest=value
            self.message.setText(f"Saved human labels locally: {self.label_path}")
        except Exception as error:self.error(error)

    def evaluate(self):
        try:
            if self.report is None:raise ValueError("Open detections first")
            value=self.labels_value();save_json(self.label_path,value)
            self.launch("evaluate",(self.detections_path,self.label_path))
        except Exception as error:self.error(error)

    def closeEvent(self,event):
        self.closing=True
        if self.worker:
            self.worker.stop();event.ignore();return
        self.closed.emit();event.accept()

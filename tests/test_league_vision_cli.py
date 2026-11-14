import os
import shlex
import sys

import pytest

import evaluate_league_vision as cli
from core.league_visual_evaluation import analyze_recording
from core.league_replay import LocalFileError,local_file


def test_missing_calibration_is_identified_before_decode_and_creates_no_output(tmp_path,monkeypatch):
    video=tmp_path/"recording.mp4";video.write_bytes(b"not decoded")
    calibration=tmp_path/"unsaved calibration.json";output=tmp_path/"review"
    def forbidden(*args):raise AssertionError("Do not decode video before calibration exists")
    monkeypatch.setattr("core.league_visual_evaluation.open_recording",forbidden)
    with pytest.raises(LocalFileError) as failure:analyze_recording(video,calibration,output)
    assert failure.value.purpose=="Calibration file"
    assert str(calibration) in str(failure.value)
    assert not output.exists() and not calibration.exists()


def test_detect_cli_missing_calibration_prints_executable_setup_instead_of_traceback(tmp_path,capsys):
    video=tmp_path/"recording.mp4";video.write_bytes(b"not decoded")
    calibration=tmp_path/"personal setup.json";output=tmp_path/"review"
    with pytest.raises(SystemExit) as failure:
        cli.main(["detect",str(video),"--calibration",str(calibration),"--output",str(output)])
    assert failure.value.code==2
    error=capsys.readouterr().err
    assert "Calibration file" in error and str(calibration) in error
    assert "label" in error and "--video" in error and "--calibration" in error
    assert "save calibration" in error and "Traceback" not in error
    assert not calibration.exists() and not output.exists()


def test_missing_video_is_not_misreported_as_missing_calibration(tmp_path,capsys):
    video=tmp_path/"absent.mp4";calibration=tmp_path/"also absent.json"
    with pytest.raises(SystemExit) as failure:
        cli.main(["detect",str(video),"--calibration",str(calibration),"--output",str(tmp_path/"review")])
    assert failure.value.code==2
    error=capsys.readouterr().err
    assert "Video recording" in error and str(video) in error
    assert "Create or repair" not in error and "Traceback" not in error


def test_setup_command_quotes_spaces_apostrophes_and_dollar_signs(tmp_path):
    video=tmp_path/"Utaris's $recording.mp4";calibration=tmp_path/"my calibration.json"
    command=cli.calibration_command(video,calibration)
    if os.name=="nt":
        assert command.startswith("& '")
        assert "'"+str(video).replace("'","''")+"'" in command
        assert "'"+str(calibration)+"'" in command
    else:
        arguments=shlex.split(command)
        assert arguments[0]==sys.executable
        assert arguments[-3:]==[str(video),"--calibration",str(calibration)]


def test_expected_input_errors_exit_cleanly_without_irrelevant_setup_hint(monkeypatch,capsys):
    def invalid(args):raise ValueError("Choose a fresh output directory")
    monkeypatch.setattr(cli,"run_operation",invalid)
    with pytest.raises(SystemExit) as failure:
        cli.main(["detect","recording.mp4","--calibration","calibration.json","--output","review"])
    assert failure.value.code==2
    error=capsys.readouterr().err
    assert "Choose a fresh output directory" in error and "Create or repair" not in error


def test_unexpected_programming_errors_are_not_hidden(monkeypatch):
    def broken(args):raise RuntimeError("unexpected failure")
    monkeypatch.setattr(cli,"run_operation",broken)
    with pytest.raises(RuntimeError,match="unexpected failure"):
        cli.main(["detect","recording.mp4","--calibration","calibration.json","--output","review"])


def test_file_validation_preserves_remote_input_rejection_and_value_error_compatibility(tmp_path):
    for remote in ("https://example.com/calibration.json",r"\\host\share\recording.mp4"):
        with pytest.raises(ValueError,match="local recorded file"):local_file(remote,purpose="Calibration file")
    with pytest.raises(ValueError) as failure:local_file(tmp_path,purpose="Calibration file")
    assert str(tmp_path) in str(failure.value)


def test_label_command_loads_video_after_showing_editor(monkeypatch):
    calls=[]
    class App:
        @classmethod
        def instance(cls):return cls()
        def exec(self):calls.append("exec");return 0
    class Dialog:
        def __init__(self,*args,**kwargs):calls.append("construct")
        def show(self):calls.append("show")
        def load_frame(self):calls.append("load_frame")
    monkeypatch.setattr("PySide6.QtWidgets.QApplication",App)
    monkeypatch.setattr("gui.league_calibration.LeagueCalibrationDialog",Dialog)
    assert cli.main(["label","--video","recording.mp4"])==0
    assert calls==["construct","show","load_frame","exec"]


def test_offline_cache_is_explicit_and_geometry_reuse_needs_a_flag(monkeypatch,tmp_path):
    calls=[]
    def detect(*args,**kwargs):
        calls.append(kwargs);return {"analyzed_frames":1,"candidates":[],"metrics":{}}
    def evaluate(*args,**kwargs):
        calls.append(kwargs);return {"accuracy":None}
    monkeypatch.setattr("core.league_visual_evaluation.analyze_recording",detect)
    monkeypatch.setattr("core.league_visual_evaluation.evaluate_labels",evaluate)
    assert cli.main(["detect","video","--calibration","profile","--output",str(tmp_path),"--cache-minimap"])==0
    assert calls[-1]["cache_minimap"] is True
    assert cli.main(["evaluate","detections","--labels","labels","--output",str(tmp_path/"score.json"),"--allow-equivalent-calibration"])==0
    assert calls[-1]["allow_equivalent_calibration"] is True

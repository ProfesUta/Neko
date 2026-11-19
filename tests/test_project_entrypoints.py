"""Relocated tools must resolve project imports without loading models/devices."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module",[
    "benchmarks.benchmark_kokoro_cancellation",
    "benchmarks.benchmark_league",
    "benchmarks.benchmark_league_live",
    "benchmarks.benchmark_league_vision_e2e",
    "benchmarks.benchmark_playback",
    "benchmarks.benchmark_recognition",
    "benchmarks.benchmark_tts",
    "benchmarks.benchmark_voice",
    "scripts.evaluate_league_vision",
    "scripts.preview_pointing",
    "scripts.preview_teleport",
])
def test_developer_tool_help_runs_from_project_root(module,tmp_path):
    result=subprocess.run([sys.executable,"-B","-m",module,"--help"],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=20,
        env={**os.environ,"CAPRI_DATA_DIR":str(tmp_path),"CAPRI_VISION":"0",
             "CAPRI_LEAGUE":"0","CAPRI_SPEECH":"0","CAPRI_PRELOAD_CHAT":"0",
             "CAPRI_LEAGUE_VISION_MODE":"OFF","QT_QPA_PLATFORM":"offscreen"})
    assert result.returncode==0,result.stderr
    assert "usage:" in result.stdout.lower()
    assert not list(tmp_path.iterdir()),"Help must not initialize persistent user data"

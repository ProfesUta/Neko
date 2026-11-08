import subprocess
import sys
import time
from threading import Event, Timer

import pytest

from core.cancellation import OperationCancelled
from voice.piper_session import PiperSession


@pytest.mark.parametrize("failure", ["slow", "crash"])
def test_resident_worker_is_reused_then_recovers_after_cancel_or_crash(tmp_path, monkeypatch, failure):
    voice = tmp_path / "voice.onnx"
    voice.write_bytes(b"model")
    voice.with_suffix(".onnx.json").write_text("{}")
    script = tmp_path / "fake_worker.py"
    script.write_text('''import json, sys, time
from pathlib import Path
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["text"] == "slow":
        time.sleep(30)
    if request["text"] == "crash":
        sys.exit(2)
    Path(request["output"]).write_bytes(b"audio")
    print(json.dumps({"ok": True}), flush=True)
''')
    real_popen = subprocess.Popen
    processes = []
    def launch(*args, **kwargs):
        process = real_popen([sys.executable, "-u", str(script)], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr("voice.piper_session.subprocess.Popen", launch)
    session = PiperSession()
    timer = None
    try:
        session.request(voice)
        output = tmp_path / "test.wav"
        session.request(voice, text="ready", output=output)
        assert len(processes) == 1 and output.read_bytes() == b"audio"
        stop = Event()
        if failure == "slow":
            timer = Timer(0.15, stop.set)
            timer.start()
        before = time.monotonic()
        with pytest.raises(OperationCancelled if failure == "slow" else RuntimeError):
            session.request(voice, stop, text=failure, output=output)
        assert time.monotonic() - before < 3
        assert processes[0].poll() is not None and session.process is None
        session.request(voice, text="recovered", output=output)
        assert len(processes) == 2 and session.process.poll() is None
    finally:
        if timer:
            timer.cancel()
            timer.join()
        session.shutdown()
    assert all(process.poll() is not None for process in processes)

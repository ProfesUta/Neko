import importlib.util
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace

import pytest

from core.cancellation import OperationCancelled
from voice.kokoro_session import KokoroSession
from voice.kokoro_worker import ASSET_FILES, CHAMPION_LEXICON, CONVERSATION_LEXICON, create_tts, synthesize, validate_assets


@pytest.fixture
def assets(tmp_path):
    directory = tmp_path / "voice"
    directory.mkdir()
    for name in ASSET_FILES:
        (directory / name).write_bytes(b"asset")
    (directory / "espeak-ng-data").mkdir()
    return directory


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    script = tmp_path / "fake_kokoro.py"
    script.write_text('''import json, sys, time
from pathlib import Path
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["text"] == "slow":
        time.sleep(30)
    if request["text"] == "crash":
        print("native runtime diagnostic", flush=True)
        sys.exit(2)
    Path(request["output"]).write_text(json.dumps(request))
    print(json.dumps({"ok": True}), flush=True)
''', encoding="utf-8")
    real_popen = subprocess.Popen
    processes = []
    commands = []

    def launch(command, **kwargs):
        commands.append(command)
        process = real_popen([sys.executable, "-u", str(script)], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("voice.kokoro_session.subprocess.Popen", launch)
    yield processes, commands
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


@pytest.mark.parametrize("failure", ["cancel", "crash", "timeout"])
def test_resident_voice_is_warmed_reused_and_recovers(assets, tmp_path, fake_worker, failure):
    processes, commands = fake_worker
    session = KokoroSession()
    timer = None
    try:
        session.request(assets)
        session.request(assets)
        assert len(processes) == 1
        assert Path(commands[0][2]).name == "kokoro_worker.py"
        assert commands[0][3] == str(assets.resolve())
        output = tmp_path / "speech.wav"
        session.request(assets, text="Capri, ready!", output=output, speaker=53, speed=1.1)
        assert json.loads(output.read_text()) == {
            "text": "Capri, ready!", "output": str(output.resolve()), "speaker": 53, "speed": 1.1,
        }
        assert len(processes) == 1
        stop = Event()
        if failure == "cancel":
            timer = Timer(0.15, stop.set)
            timer.start()
        before = time.monotonic()
        with pytest.raises(OperationCancelled if failure == "cancel" else RuntimeError) as error:
            session.request(assets, stop, timeout=0.2 if failure == "timeout" else 5,
                            text="crash" if failure == "crash" else "slow", output=output)
        assert time.monotonic() - before < 3
        if failure == "crash":
            assert "Kokoro" in str(error.value) and "native runtime diagnostic" in str(error.value)
        assert session.process is None and processes[0].poll() is not None
        session.request(assets, text="Recovered", output=output)
        assert len(processes) == 2 and session.process.poll() is None
    finally:
        if timer:
            timer.cancel()
            timer.join()
        session.shutdown()
    assert all(process.poll() is not None for process in processes)


def test_asset_change_reloads_voice(assets, fake_worker):
    processes, _ = fake_worker
    session = KokoroSession()
    try:
        session.request(assets)
        (assets / "voices.bin").write_bytes(b"new voices")
        session.request(assets)
        assert len(processes) == 2
        assert processes[0].poll() is not None
    finally:
        session.shutdown()


def test_pronunciation_dictionary_change_reloads_warm_worker(assets, fake_worker, tmp_path, monkeypatch):
    dictionary = tmp_path / "champions.lexicon"
    dictionary.write_text("ahri a r i\n", encoding="utf-8")
    monkeypatch.setattr("voice.kokoro_session.CHAMPION_LEXICON", dictionary)
    processes, _ = fake_worker
    session = KokoroSession()
    try:
        session.request(assets)
        dictionary.write_text("ahri a r i\nyuumi j u m i\n", encoding="utf-8")
        session.request(assets)
        assert len(processes) == 2 and processes[0].poll() is not None
    finally:
        session.shutdown()


def test_stop_terminates_windows_launcher_children(assets, fake_worker, tmp_path):
    import psutil

    session = KokoroSession()
    stop, timer, children = Event(), None, []
    try:
        session.request(assets)
        children = psutil.Process(session.process.pid).children(recursive=True)
        timer = Timer(.15, stop.set)
        timer.start()
        with pytest.raises(OperationCancelled):
            session.request(assets, stop, text="slow", output=tmp_path / "cancelled.wav")
        _, alive = psutil.wait_procs(children, timeout=1)
        assert not alive, "Stop left a speech worker running"
    finally:
        if timer:
            timer.cancel()
            timer.join()
        session.shutdown()
        for child in children:
            try:
                if child.is_running():
                    child.kill()
                    child.wait(timeout=2)
            except psutil.NoSuchProcess:
                pass


@pytest.mark.parametrize("arguments", [
    {"speaker": -1}, {"speaker": 1024}, {"speaker": True}, {"speaker": 1.5},
    {"speed": float("nan")}, {"speed": float("inf")}, {"speed": 0}, {"speed": True}, {"speed": 10 ** 1000},
    {"timeout": float("nan")}, {"timeout": 0}, {"timeout": True}, {"timeout": 10 ** 1000},
    {"text": ""}, {"text": "\x00bad"}, {"text": "a" * 10001}, {"text": 42},
    {"text": "Hello", "output": None},
])
def test_invalid_requests_do_not_launch_worker(assets, fake_worker, arguments):
    session = KokoroSession()
    with pytest.raises(ValueError):
        session.request(assets, **arguments)
    assert not fake_worker[0] and session.process is None


def test_missing_asset_does_not_launch_worker(assets, fake_worker):
    (assets / "tokens.txt").unlink()
    session = KokoroSession()
    with pytest.raises(ValueError, match="tokens.txt"):
        session.request(assets)
    assert not fake_worker[0]


def test_cancelled_request_never_launches(assets, fake_worker):
    stop = Event()
    stop.set()
    session = KokoroSession()
    with pytest.raises(OperationCancelled):
        session.request(assets, stop)
    assert not fake_worker[0]


def test_cancelling_wait_for_busy_session_preserves_the_warm_worker(assets, fake_worker):
    session = KokoroSession()
    stop = Event()
    timer = Timer(0.1, stop.set)
    try:
        session.request(assets)
        process = session.process
        session._lock.acquire()
        timer.start()
        try:
            with pytest.raises(OperationCancelled):
                session.request(assets, stop)
        finally:
            session._lock.release()
        assert session.process is process and process.poll() is None
    finally:
        timer.cancel()
        timer.join()
        session.shutdown()


@pytest.mark.parametrize("message", [{"ok": True}, {"error": "invalid voice pack"}])
def test_worker_must_confirm_readiness(assets, tmp_path, monkeypatch, message):
    script = tmp_path / "bad_startup.py"
    script.write_text(f"import time\nprint({json.dumps(message)!r}, flush=True)\ntime.sleep(30)\n")
    real_popen = subprocess.Popen
    processes = []

    def launch(command, **kwargs):
        process = real_popen([sys.executable, "-u", str(script)], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("voice.kokoro_session.subprocess.Popen", launch)
    session = KokoroSession()
    try:
        with pytest.raises(RuntimeError, match="Kokoro"):
            session.request(assets)
        assert session.process is None and processes[0].poll() is not None
    finally:
        session.shutdown()


def test_synthesis_writes_pcm16_at_the_models_sample_rate(tmp_path):
    import numpy as np

    calls = []

    def generate(text, **kwargs):
        calls.append((text, kwargs))
        return SimpleNamespace(samples=np.array([-2, -0.5, 0, 0.5, 2]), sample_rate=24000)

    tts = SimpleNamespace(num_speakers=54, generate=generate)
    output = tmp_path / "speech.wav"
    result = synthesize(tts, {"text": "Hey, chum!", "output": str(output)})
    assert calls == [("Hey, chum!", {"sid": 53, "speed": 1.1})]
    assert result == {"ok": True, "sample_rate": 24000, "frames": 5}
    with wave.open(str(output)) as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (1, 2, 24000)
        assert np.frombuffer(stream.readframes(5), dtype="<i2").tolist() == [-32767, -16383, 0, 16383, 32767]
    with pytest.raises(ValueError, match="unavailable"):
        synthesize(tts, {"text": "Hey", "output": str(output), "speaker": 54})
    assert len(calls) == 1


def test_runtime_config_uses_pack_data_and_installed_cpu_runtime(assets, monkeypatch):
    received = {}

    def config(name):
        def build(**kwargs):
            received[name] = kwargs
            return SimpleNamespace(validate=lambda: True)
        return build

    runtime = SimpleNamespace(
        OfflineTtsKokoroModelConfig=config("kokoro"),
        OfflineTtsModelConfig=config("model"),
        OfflineTtsConfig=config("config"),
        OfflineTts=lambda settings: settings,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", runtime)
    create_tts(assets)
    assert received["kokoro"]["lang"] == "en-us"
    assert received["kokoro"]["data_dir"] == str(assets / "espeak-ng-data")
    assert received["kokoro"]["lexicon"].split(",") == [str(assets / "lexicon-us-en.txt"), str(CONVERSATION_LEXICON), str(CHAMPION_LEXICON)]
    assert received["model"]["provider"] == "cpu" and received["model"]["num_threads"] == 2
    assert received["config"]["max_num_sentences"] == 1


def test_real_kokoro_pack_generates_without_playback_when_installed(tmp_path):
    directory = Path(os.getenv("CAPRI_KOKORO_TEST_ASSET_DIR", str(
        Path(__file__).resolve().parents[1] / "assets" / "voices" / "capri-kokoro")))
    if importlib.util.find_spec("sherpa_onnx") is None:
        pytest.skip("Optional sherpa-onnx runtime is not installed")
    try:
        validate_assets(directory)
    except ValueError:
        pytest.skip("Optional official Capri voice pack is not installed")
    session = KokoroSession()
    try:
        session.request(directory)
        process = session.process
        output = tmp_path / "real-capri.wav"
        session.request(directory, text="Hey, chum. I'm right here.", output=output)
        assert session.process is process
        with wave.open(str(output)) as stream:
            assert stream.getsampwidth() == 2 and stream.getnchannels() == 1
            assert stream.getframerate() == 24000 and stream.getnframes() > 2400
    finally:
        session.shutdown()

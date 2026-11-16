"""Protocol/process cancellation tests without a native model or audio device."""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event,Timer

import numpy as np
import pytest

from core.cancellation import OperationCancelled
from voice.kokoro_session import KokoroSession,speech_priority
from voice.kokoro_worker import synthesize


@pytest.fixture
def worker(tmp_path,monkeypatch):
    from voice.kokoro_worker import ASSET_FILES
    assets=tmp_path/"assets";assets.mkdir()
    for name in ASSET_FILES:(assets/name).write_bytes(b"fixture")
    (assets/"espeak-ng-data").mkdir()
    script=tmp_path/"protocol.py";log=tmp_path/"requests.jsonl"
    script.write_text('''import json,sys,time
from pathlib import Path
time.sleep(.12)
print(json.dumps({"ready":True,"protocol":2,"cancel_callback":True}),flush=True)
retired=None
for line in sys.stdin:
 r=json.loads(line);key=r['request_id'];out=Path(r['output']);text=r['text']
 with open(sys.argv[1],'a') as f:f.write(json.dumps(r)+'\\n')
 def emit(v):print(json.dumps({**v,'request_id':key}),flush=True)
 def chunk(i):
  p=out.with_name(out.stem+'-'+str(i)+'.wav');p.write_text(text+'-'+str(i))
  emit({'chunk':str(p),'chunk_index':i,'generation_complete':False})
 if text=='hang':time.sleep(30)
 if text in ('slow','partial','crash-partial'):
  if text!='slow':chunk(0)
  time.sleep(.18)
 if text=='crash-partial':sys.exit(2)
 if text=='missing':print(json.dumps({'ok':True}),flush=True);continue
 if text=='spoof-exit':print(json.dumps({'ok':True,'worker_exited':True}),flush=True);continue
 if text=='mismatch':print(json.dumps({'ok':True,'request_id':'wrong'}),flush=True);continue
 if text=='bad-index':
  p=out.with_name('audio-1.wav');p.write_text('bad')
  emit({'chunk':str(p),'chunk_index':1});continue
 if text=='late' and retired:
  print(json.dumps({'ok':True,'request_id':retired}),flush=True)
  print(json.dumps({'chunk':str(out),'chunk_index':0,'request_id':retired}),flush=True)
 if r.get('cancel_path') and Path(r['cancel_path']).exists():
  emit({'cancelled':True});retired=key;continue
 if r.get('stream'):
  chunk(1 if text=='partial' else 0)
 else:out.write_text(text)
 emit({'ok':True});retired=key
''',encoding="utf-8")
    real=subprocess.Popen;processes=[]
    def launch(command,**options):
        p=real([sys.executable,"-u",str(script),str(log)],**options);processes.append(p);return p
    monkeypatch.setattr("voice.kokoro_session.subprocess.Popen",launch)
    yield assets,log,processes
    for p in processes:
        if p.poll() is None:p.kill()
        p.wait(timeout=2)


def wait_until(condition,timeout=2):
    end=time.monotonic()+timeout
    while not condition():
        if time.monotonic()>end:raise AssertionError("Condition not reached")
        time.sleep(.005)


@pytest.mark.parametrize("policy",["drain","cooperative"])
def test_cancelled_full_synthesis_is_drained_without_touching_destroyed_caller_directory(worker,policy):
    assets,log,processes=worker;session=KokoroSession(policy);stop=Event()
    try:
        session.request(assets);pid=session.process.pid
        with TemporaryDirectory() as caller,ThreadPoolExecutor(1) as pool:
            f=pool.submit(session.request,assets,stop,text="slow",output=Path(caller)/"old.wav")
            wait_until(log.exists);stop.set()
            with pytest.raises(OperationCancelled):f.result(timeout=.2)
            assert session.snapshot()["state"]=="draining"
        new=assets.parent/"new.wav"
        session.request(assets,text="What's on your mind?",output=new)
        assert new.read_text()=="What's on your mind?" and session.process.pid==pid and len(processes)==1
        requests=[json.loads(line) for line in log.read_text().splitlines()]
        assert all(not Path(r["output"]).parent.exists() for r in requests)
        assert session.snapshot()["drained"]==1
    finally:session.shutdown()


def test_old_chunks_and_late_completion_cannot_satisfy_the_next_utterance(worker):
    assets,log,_=worker;session=KokoroSession("cooperative");stop=Event();seen=[]
    try:
        session.request(assets)
        def ready(m):seen.append(Path(m["chunk"]).read_text());stop.set()
        with pytest.raises(OperationCancelled):
            session.request(assets,stop,text="partial",output=assets.parent/"old.wav",on_chunk=ready)
        output=assets.parent/"new.wav"
        session.request(assets,text="late",output=output)
        assert seen==["partial-0"] and output.read_text()=="late"
        assert session.snapshot()["starts"]==1
    finally:session.shutdown()


@pytest.mark.parametrize("fault",["missing","mismatch","bad-index","crash-partial","spoof-exit"])
def test_invalid_native_protocol_restarts_once_then_accepts_a_new_request(worker,fault):
    assets,_,processes=worker;session=KokoroSession()
    try:
        session.request(assets)
        with pytest.raises(RuntimeError):
            session.request(assets,text=fault,output=assets.parent/"bad.wav",on_chunk=lambda m:None)
        assert session.process is None
        session.request(assets,text="All words preserved.",output=assets.parent/"new.wav")
        assert len(processes)==2 and (assets.parent/"new.wav").read_text()=="All words preserved."
    finally:session.shutdown()


@pytest.mark.parametrize("shutdown",[False,True])
def test_cancelled_model_load_finishes_once_or_shuts_down_cleanly(worker,shutdown):
    assets,_,processes=worker;session=KokoroSession();stop=Event()
    try:
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(session.request,assets,stop)
            wait_until(lambda:session.process is not None)
            stop.set()
            with pytest.raises(OperationCancelled):future.result(timeout=.2)
        if shutdown:
            session.shutdown();assert session.process is None
        else:
            session.request(assets)
            assert len(processes)==1 and session.snapshot()["state"]=="ready"
    finally:session.shutdown()
    assert all(p.poll() is not None for p in processes)


def test_hung_abandoned_generation_has_bounded_restart_and_file_cleanup(worker,monkeypatch):
    assets,log,processes=worker;session=KokoroSession();stop=Event()
    monkeypatch.setattr("config.KOKORO_ABANDON_TIMEOUT_SECONDS",.05)
    try:
        session.request(assets)
        with ThreadPoolExecutor(1) as pool:
            f=pool.submit(session.request,assets,stop,text="hang",output=assets.parent/"old.wav")
            wait_until(log.exists);old=json.loads(log.read_text().splitlines()[0]);stop.set()
            with pytest.raises(OperationCancelled):f.result(timeout=.2)
        session.request(assets,text="Recovered",output=assets.parent/"new.wav")
        assert len(processes)==2 and not Path(old["output"]).parent.exists()
        assert (assets.parent/"new.wav").read_text()=="Recovered"
    finally:session.shutdown()


def test_shutdown_interrupts_a_drain_instead_of_waiting_for_its_deadline(worker):
    assets,log,_=worker;session=KokoroSession();stop=Event()
    try:
        session.request(assets)
        with ThreadPoolExecutor(1) as pool:
            f=pool.submit(session.request,assets,stop,text="hang",output=assets.parent/"old.wav")
            wait_until(log.exists);stop.set()
            with pytest.raises(OperationCancelled):f.result(timeout=.2)
        begin=time.monotonic();session.shutdown()
        assert time.monotonic()-begin<.5 and session.process is None and session._drainer is None
    finally:session.shutdown()


def test_cancelled_startup_timeout_is_bounded_and_next_model_load_can_succeed(worker,monkeypatch):
    assets,_,processes=worker;session=KokoroSession();stop=Event()
    monkeypatch.setattr("config.KOKORO_STARTUP_DRAIN_SECONDS",.025)
    try:
        with ThreadPoolExecutor(1) as pool:
            f=pool.submit(session.request,assets,stop)
            wait_until(lambda:session.process is not None);stop.set()
            with pytest.raises(OperationCancelled):f.result(timeout=.2)
        session.request(assets)
        assert len(processes)==2 and session.snapshot()["state"]=="ready"
    finally:session.shutdown()


def test_expired_notification_waiter_cannot_steal_the_slot_or_restart_a_draining_worker(worker):
    from core.cancellation import DeadlineEvent
    assets,log,_=worker;session=KokoroSession("drain");stop=Event()
    try:
        session.request(assets)
        with ThreadPoolExecutor(1) as pool:
            f=pool.submit(session.request,assets,stop,text="slow",output=assets.parent/"old.wav")
            wait_until(log.exists);stop.set()
            with pytest.raises(OperationCancelled):f.result(timeout=.2)
        with speech_priority(0),pytest.raises(OperationCancelled):
            session.request(assets,DeadlineEvent(time.monotonic()+.02),text="expired",output=assets.parent/"expired.wav")
        session.request(assets,text="user",output=assets.parent/"new.wav")
        assert [json.loads(s)["text"] for s in log.read_text().splitlines()]==["slow","user"]
        assert session.snapshot()["starts"]==1
    finally:session.shutdown()


def test_shutdown_invalidates_already_waiting_requests_even_after_closing_flag_clears(worker):
    assets,_,_=worker;session=KokoroSession()
    try:
        session.request(assets);session._lock.acquire()
        with ThreadPoolExecutor(2) as pool:
            waiting=pool.submit(session.request,assets,text="queued",output=assets.parent/"queued.wav")
            wait_until(lambda:session.snapshot()["waiting"]==1)
            closing=pool.submit(session.shutdown)
            wait_until(session._closing.is_set);session._lock.release()
            closing.result(timeout=2)
            with pytest.raises(OperationCancelled):waiting.result(timeout=2)
        assert session.process is None and session.snapshot()["starts"]==1
    finally:session.shutdown()


def test_user_request_wins_over_waiting_low_priority_notifications(worker):
    assets,log,_=worker;session=KokoroSession("drain");stop=Event()
    try:
        session.request(assets)
        with ThreadPoolExecutor(3) as pool:
            old=pool.submit(session.request,assets,stop,text="slow",output=assets.parent/"old.wav")
            wait_until(log.exists);stop.set()
            with pytest.raises(OperationCancelled):old.result(timeout=.2)
            def speak(text,priority):
                with speech_priority(priority):session.request(assets,text=text,output=assets.parent/(text+".wav"))
            low=pool.submit(speak,"notice",0)
            wait_until(lambda:session.snapshot()["waiting"]==1)
            high=pool.submit(speak,"user",10)
            high.result(timeout=2);low.result(timeout=2)
        assert [json.loads(s)["text"] for s in log.read_text().splitlines()]==["slow","user","notice"]
    finally:session.shutdown()


def test_rapid_cancelled_waiters_do_not_queue_native_jobs_or_destroy_warm_engine(worker):
    assets,log,_=worker;session=KokoroSession("drain");stop=Event()
    try:
        session.request(assets)
        with ThreadPoolExecutor(1) as pool:
            f=pool.submit(session.request,assets,stop,text="slow",output=assets.parent/"old.wav")
            wait_until(log.exists);stop.set()
            with pytest.raises(OperationCancelled):f.result(timeout=.2)
        for _ in range(6):
            token=Event();token.set()
            with pytest.raises(OperationCancelled):session.request(assets,token,text="obsolete",output=assets.parent/"unused.wav")
        session.request(assets,text="user",output=assets.parent/"new.wav")
        assert session.snapshot()["starts"]==1 and len(log.read_text().splitlines())==2
    finally:session.shutdown()


def test_cancellation_after_queue_pop_preserves_the_terminal_ack_for_drain(worker,monkeypatch):
    assets,_,_=worker;session=KokoroSession();stop=Event()
    try:
        session.request(assets);process=session.process;original=session._messages.get;intercepted=[]
        def get(*a,**kw):
            message=original(*a,**kw)
            if message.get("ok") and not intercepted:intercepted.append(True);stop.set()
            return message
        monkeypatch.setattr(session._messages,"get",get)
        with pytest.raises(OperationCancelled):
            session.request(assets,stop,text="old",output=assets.parent/"old.wav")
        session.request(assets,text="new",output=assets.parent/"new.wav")
        assert not (assets.parent/"old.wav").exists() and (assets.parent/"new.wav").read_text()=="new"
        assert session.process is process and session.snapshot()["starts"]==1
    finally:session.shutdown()


def test_runtime_without_advertised_stop_semantics_uses_bounded_drain(worker):
    assets,log,_=worker;session=KokoroSession();stop=Event()
    try:
        session.request(assets);session._cancel_callback=False
        with ThreadPoolExecutor(1) as pool:
            f=pool.submit(session.request,assets,stop,text="slow",output=assets.parent/"old.wav")
            wait_until(log.exists);stop.set()
            with pytest.raises(OperationCancelled):f.result(timeout=.2)
        session.request(assets,text="new",output=assets.parent/"new.wav")
        assert "cancel_path" not in json.loads(log.read_text().splitlines()[0])
        assert session.snapshot()["starts"]==1
    finally:session.shutdown()


@pytest.mark.parametrize("duration",[0,-1,float("nan"),float("inf"),True,10**1000])
def test_invalid_abandon_policy_never_launches_or_touches_native_files(worker,monkeypatch,duration):
    assets,_,processes=worker;session=KokoroSession()
    monkeypatch.setattr("config.KOKORO_ABANDON_TIMEOUT_SECONDS",duration)
    with pytest.raises(ValueError):session.request(assets)
    assert processes==[]


def test_native_cooperative_stop_discards_partial_pcm_and_normal_callback_preserves_samples(tmp_path):
    from types import SimpleNamespace
    import wave
    values=np.array([-.2,0,.2],dtype=np.float32);marker=tmp_path/"cancel";calls=[]
    def generate(text,callback=None,**kwargs):
        for progress in (.5,1.):
            calls.append(progress)
            if callback and callback(values,progress)==0:break
        return SimpleNamespace(samples=np.tile(values,len(calls)),sample_rate=24000)
    native=SimpleNamespace(num_speakers=54,sample_rate=24000,generate=generate)
    output=tmp_path/"normal.wav"
    synthesize(native,{"text":"What's on your mind?","output":output,"cancel_path":str(marker)})
    with wave.open(str(output)) as src:
        assert np.frombuffer(src.readframes(src.getnframes()),dtype="<i2").tolist()==[-6553,0,6553]*2
    calls.clear()
    def delivered(m):marker.touch()
    result=synthesize(native,{"text":"What's on your mind?","output":tmp_path/"old.wav","cancel_path":str(marker)},delivered)
    assert result=={"cancelled":True} and not (tmp_path/"old.wav").exists()

"""Explicit fixed-fixture Kokoro recovery tests. No League, microphone or LLM.

--audio authorizes actual Capri speech; --loopback observes only the render mix
in RAM. Other output must be quiet. Timing is digital, never acoustic output.
"""
import argparse
import csv
import hashlib
import json
import time
import wave
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import config
from core.cancellation import OperationCancelled
from core.latency import LatencyTrace,VoiceMetrics,trace_scope
from voice.kokoro_session import KokoroSession,speech_priority
from voice import kokoro_session,tts
from benchmarks.league_vision_e2e import distribution,Resources,attach_loopback

CASES=("before","native","chunk","complete","immediate","rapid","notifications","startup","startup-late","playback","normal","fault")
OLD="Hello there. What's on your mind? I am here if you need anything."
NEW="What's on your mind?"


class Trace(LatencyTrace):
    def __init__(self,session,dispatched=None):
        super().__init__(VoiceMetrics(True));self.session=session;self.dispatched=dispatched;self.ready_ns=None

    def mark(self,name,at=None):
        super().mark(name,at)
        if name=="tts_worker_dispatched":
            self.ready_ns=self.session.snapshot()["ready_ns"]
            if self.dispatched:self.dispatched.set()


def pcm_digest(path):
    with wave.open(str(path)) as source:
        pcm=source.readframes(source.getnframes())
        return {"sha256":hashlib.sha256(pcm).hexdigest(),"frames":source.getnframes(),"rate":source.getframerate()}


def trial(session,case,*,audio=False,probe=None,save=None):
    declared_case=case
    if case=="fault":case="immediate"
    if case=="startup-late":case="startup"
    stop=Event();dispatched=Event();started=Event();old_trace=Trace(session,dispatched);new_trace=Trace(session)
    row={"case":declared_case,"policy":session.cancellation_policy,"loopback_error":None}
    if declared_case=="fault":row["fixture"]="Injected Python worker stall before native generate; follow-up uses real Kokoro"
    resources=Resources();begin=time.perf_counter_ns();old_error=[];cancel_ns=None
    def await_future(future):
        while not future.done():resources.sample();time.sleep(.005)
        return future.result()
    def cancel():
        nonlocal cancel_ns
        snapshot=session.snapshot();row["state_at_cancel"]=snapshot["state"]
        row["obsolete_request_id"]=snapshot["request_id"]
        cancel_ns=time.perf_counter_ns();old_trace.mark("interruption_detected",cancel_ns);stop.set()
    with TemporaryDirectory(prefix="capri-cancellation-test-") as directory,ThreadPoolExecutor(2) as pool:
        output=Path(directory)/"old.wav"
        def obsolete():
            try:
                with trace_scope(old_trace),speech_priority(0):
                    if case=="playback":
                        tts.speak("Benchmark old reply. This speech will be interrupted.",stop,on_playback_start=started.set)
                    else:
                        def chunk(message):
                            row.setdefault("old_chunk_indices",[]).append(message.get("chunk_index"))
                            cancel()
                        session.request(config.KOKORO_VOICE_DIR,stop,text=OLD if case!="complete" else NEW,output=output,
                            speaker=config.KOKORO_SPEAKER_ID,speed=config.KOKORO_SPEED,
                            on_chunk=chunk if case in ("chunk","complete") else None)
            except OperationCancelled:pass
            except Exception as error:old_error.append(type(error).__name__)
            finally:row["old_call_returned_ns"]=time.perf_counter_ns()
        initial=session.snapshot();row["initial_pid"]=initial["pid"];row["starts_before"]=initial["starts"]
        if case=="startup":
            session.shutdown();row["fixture"]="Cold startup cancellation, not a warm inference sample"
        if case not in ("normal","notifications"):
            if case=="before":cancel()
            old=pool.submit(obsolete)
            if case not in ("before","chunk","complete"):
                until=time.monotonic()+20
                while True:
                    target=(session.process is not None if case=="startup" else started.is_set() if case=="playback" else dispatched.is_set())
                    if target:break
                    if old.done():await_future(old);raise RuntimeError("Old fixture failed before cancellation point")
                    if time.monotonic()>until:raise RuntimeError("Fixture did not reach its cancellation point")
                    resources.sample();time.sleep(.002)
                if case=="startup":row["initial_pid"]=session.process.pid
                time.sleep(1. if declared_case=="startup-late" else .05 if case=="startup" else .1);cancel()
            if case in ("chunk","complete"):
                until=time.monotonic()+20
                while not stop.is_set():
                    if old.done():await_future(old);raise RuntimeError("Fixture produced no expected native chunk")
                    if time.monotonic()>until:raise RuntimeError("Chunk fixture timeout")
                    resources.sample();time.sleep(.005)
            if case not in ("immediate","startup"):
                await_future(old)
            if case=="native":
                # This case measures recovery first; immediate separately keeps
                # the new request queued throughout the obsolete operation.
                await_future(pool.submit(session.request,config.KOKORO_VOICE_DIR))
                row["observed_worker_ready_ns"]=time.perf_counter_ns()
            if case=="rapid":
                from threading import Timer
                for _ in range(2):
                    event=Event();timer=Timer(.025,event.set);timer.start()
                    try:
                        session.request(config.KOKORO_VOICE_DIR,event,text=OLD,output=output)
                    except OperationCancelled:pass
                    finally:timer.cancel();timer.join()
        if cancel_ns is None:cancel_ns=time.perf_counter_ns();row["control_without_cancellation"]=True
        row["cancel_ns"]=cancel_ns
        def response():
            with trace_scope(new_trace):
                speech=tts.prepare_speech(NEW)
                if speech is None:raise RuntimeError("No real response PCM generated")
                with speech:
                    row["new_pcm"]=pcm_digest(speech.path)
                    if save:
                        import shutil
                        save.mkdir(parents=True,exist_ok=True)
                        shutil.copyfile(speech.path,save/(declared_case+"-"+session.cancellation_policy+".wav"))
                    if audio:
                        if probe:
                            try:probe.arm()
                            except RuntimeError as error:row["loopback_error"]=str(error)
                        speech.play()
                if case=="notifications":
                    for phrase in ("Warwick is visible.","Yuumi is locked in."):
                        extra=tts.prepare_speech(phrase)
                        with extra:
                            if audio:extra.play()
        row["request_begin_ns"]=time.perf_counter_ns()
        await_future(pool.submit(response))
        if case not in ("normal","notifications"):await_future(old)
        marks=new_trace.snapshot();old_marks=old_trace.snapshot()
        row.update(dispatch_ns=marks.get("tts_worker_dispatched"),pcm_ns=marks.get("audio_ready"),
                   T9=marks.get("audio_ready"),T10=marks.get("device_submitted"),T11=None,
                   dac_ns=marks.get("device_playback"),old_abort_ns=old_marks.get("output_abort_complete"),
                   worker_ready_ns=new_trace.ready_ns,finished_ns=time.perf_counter_ns(),old_errors=old_error)
        if probe and audio and not row["loopback_error"]:
            attach_loopback(row,probe.result(.1))
        final=session.snapshot();row["starts_after"]=final["starts"];row["final_pid"]=final["pid"]
        row["starts_during_recovery"]=final["starts"]-row["starts_before"]
        drain=final["last_drain"]
        row["last_drain"]=drain if drain and drain["finished_ns"]>=cancel_ns else None
    row["resources"]=resources.report();row["wall_ms"]=(time.perf_counter_ns()-begin)/1e6
    return row


def stalled_worker(output):
    """Private fault injection; production worker and model assets are untouched."""
    path=output/"controlled_stall.py"
    path.write_text("import sys,time\n"+f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        +"from voice import kokoro_worker as worker\noriginal=worker.synthesize\n"
        +f"def stalled(engine,request,on_chunk=None):\n if request.get('text')=={OLD!r}:time.sleep(30)\n return original(engine,request,on_chunk)\n"
        +"worker.synthesize=stalled\nworker.main()\n",encoding="utf-8")
    return path.resolve()


def native_probe(output,save=None):
    """One real-runtime contract check, not a latency benchmark or listening score."""
    import numpy as np,sherpa_onnx
    from voice.kokoro_worker import create_tts,callback_cancellation_supported
    personal=config.PERSONAL_PRONUNCIATIONS_FILE
    engine=create_tts(config.KOKORO_VOICE_DIR,config.KOKORO_NUM_THREADS,config.KOKORO_CPU_OPTIONS,
                      personal if personal and Path(personal).is_file() else None)
    records={};settings={"sid":config.KOKORO_SPEAKER_ID,"speed":config.KOKORO_SPEED}
    for name,stop in (("continue",False),("stop",True)):
        callbacks=[]
        def received(samples,progress):
            callbacks.append({"frames":len(samples),"progress":float(progress)})
            return 0 if stop else 1
        audio=engine.generate(OLD,callback=received,**settings)
        records[name]={"callbacks":callbacks,"returned_frames":len(audio.samples)}
    for name,callback in (("normal",None),("callback-normal",lambda s,p:1),("after-stop",None)):
        audio=engine.generate(NEW,callback=callback,**settings)
        records[name]={"frames":len(audio.samples),"sample_rate":audio.sample_rate}
        if save:
            save.mkdir(parents=True,exist_ok=True)
            with wave.open(str(save/("probe-"+name+".wav")),"wb") as f:
                f.setparams((1,2,audio.sample_rate,0,"NONE","not compressed"))
                f.writeframes((np.clip(np.asarray(audio.samples),-1,1)*32767).astype("<i2").tobytes())
    records.update(runtime=sherpa_onnx.__version__,documented_stop=callback_cancellation_supported(),
        stopped_early=len(records["stop"]["callbacks"])<len(records["continue"]["callbacks"])
                      and records["stop"]["returned_frames"]<records["continue"]["returned_frames"],
        measurement="One native contract check. Generated audio is not a subjective word/pronunciation verdict.")
    output.mkdir(parents=True,exist_ok=True)
    (output/"native-probe.json").write_text(json.dumps(records,indent=2),encoding="utf-8")
    print(json.dumps(records));return 0 if records["stopped_early"] else 1


def summary(rows):
    result={}
    for case in sorted({r["case"] for r in rows}):
        result[case]={}
        for policy in sorted({r["policy"] for r in rows}):
            selected=[r for r in rows if r["case"]==case and r["policy"]==policy and not r["warmup"]]
            if not selected:continue
            metrics={}
            for name,end in (("cancel_to_old_return","old_call_returned_ns"),("cancel_to_worker_ready","worker_ready_ns"),
                ("cancel_to_dispatch","dispatch_ns"),("cancel_to_pcm","pcm_ns"),("cancel_to_digital_mix","T11"),
                ("cancel_to_software_abort","old_abort_ns")):
                values=[(r[end]-r["cancel_ns"])/1e6 for r in selected if r.get(end) and r[end]>=r["cancel_ns"]]
                metrics[name]=distribution(values,len(selected)-len(values))
            metrics["request_to_pcm"]=distribution([(r["pcm_ns"]-r["request_begin_ns"])/1e6 for r in selected if r.get("pcm_ns")])
            result[case][policy]={"trials":len(selected),"metrics":metrics,
                "restarts":sum(r.get("starts_during_recovery",0) for r in selected),
                "reused":sum(r.get("initial_pid")==r.get("final_pid") for r in selected),
                "errors":sum(bool(r.get("error") or r.get("old_errors")) for r in selected),
                "pcm_hashes":sorted({r["new_pcm"]["sha256"] for r in selected if r.get("new_pcm")})}
    return result


def write(output,rows,metadata):
    simple=[{k:v for k,v in r.items() if not isinstance(v,(dict,list))} for r in rows]
    fields=list(dict.fromkeys(k for r in simple for k in r))
    with (output/"trials.csv").open("w",newline="",encoding="utf-8") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(simple)
    (output/"report.json").write_text(json.dumps({"metadata":metadata,"summary":summary(rows),"trials":rows},indent=2),encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples",type=int,default=20);parser.add_argument("--warmups",type=int,default=1)
    parser.add_argument("--cases",default=",".join(CASES))
    parser.add_argument("--policies",default="restart,drain,cooperative")
    parser.add_argument("--audio",action="store_true");parser.add_argument("--loopback",action="store_true")
    parser.add_argument("--save-audio",type=Path);parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--native-probe",action="store_true",help="Isolated direct-runtime callback contract check; no playback")
    args=parser.parse_args()
    cases=args.cases.split(",");policies=args.policies.split(",")
    if not 1<=args.samples<=100 or not 0<=args.warmups<=3:parser.error("Use 1..100 samples and 0..3 warm-ups")
    if not set(cases)<=set(CASES) or not set(policies)<={"restart","drain","cooperative"}:parser.error("Unknown case/policy")
    if args.loopback and not args.audio:parser.error("Loopback requires --audio")
    if args.native_probe:return native_probe(args.output,args.save_audio)
    if "playback" in cases and not args.audio:cases.remove("playback")
    args.output.mkdir(parents=True,exist_ok=True)
    if (args.output/"report.json").exists():parser.error("Choose a fresh output folder")
    from contextlib import ExitStack
    import platform,sherpa_onnx
    metadata={"recorded_at_utc":datetime.now(timezone.utc).isoformat(),"python":platform.python_version(),"platform":platform.platform(),
        "runtime":sherpa_onnx.__version__,"audio":args.audio,"loopback":args.loopback,
        "voice":{"speaker":config.KOKORO_SPEAKER_ID,"speed":config.KOKORO_SPEED,"threads":config.KOKORO_NUM_THREADS},
        "drain_seconds":config.KOKORO_ABANDON_TIMEOUT_SECONDS,"startup_seconds":config.KOKORO_STARTUP_DRAIN_SECONDS,
        "measurement":"Real Kokoro; controlled cancellation. PCM, software abort, DAC estimate and digital mix are not acoustic output."}
    rows=[];session=KokoroSession();original=kokoro_session.session;kokoro_session.session=session
    try:
        with ExitStack() as stack:
            stack.callback(session.shutdown);probe=None
            if args.loopback:
                from benchmarks.wasapi_loopback import LoopbackProbe
                probe=stack.enter_context(LoopbackProbe());time.sleep(.25)
                metadata["loopback_format"]=probe.format
            for case in cases:
                session.shutdown()
                session.worker_filename=str(stalled_worker(args.output)) if case=="fault" else "kokoro_worker.py"
                for pair in range(-args.warmups,args.samples):
                    for policy in (policies if pair%2 else list(reversed(policies))):
                        session.cancellation_policy=policy
                        previous=session.process
                        session.request(config.KOKORO_VOICE_DIR)
                        if previous is not session.process:
                            prepared=tts.prepare_speech(NEW)
                            with prepared:pass
                        try:row=trial(session,case,audio=args.audio,probe=probe,save=args.save_audio)
                        except Exception as error:
                            row={"case":case,"policy":policy,"error":type(error).__name__+": "+str(error)}
                            session.shutdown()
                        row.update(pair=pair,warmup=pair<0)
                        rows.append(row);write(args.output,rows,metadata)
                        print(json.dumps({"case":case,"policy":policy,"pair":pair,"restarts":row.get("starts_during_recovery"),"error":row.get("error")}),flush=True)
    finally:
        kokoro_session.session=original;session.shutdown()
        write(args.output,rows,metadata)


if __name__=="__main__":main()

"""Optional, finite benchmark-only model load and NVIDIA sampling.

No service startup, model selection change, screenshot upload or live capture.
The real admission/cancellation system remains in control of background vision.
"""
import csv
import io
from collections import deque
import shutil
import subprocess
import time
from threading import Event,Lock,Thread
from urllib.parse import urlparse

import config


class NvidiaSamples:
    def __init__(self,path,enabled=False):
        self.path=path;self.process=None;self.stream=None;self.error=None;self.enabled=enabled;self.result=None
        if not enabled:return
        executable=shutil.which("nvidia-smi")
        if not executable:self.error="nvidia-smi unavailable";return
        self.stream=path.open("w",encoding="utf-8")
        try:
            self.process=subprocess.Popen([executable,"--query-gpu=timestamp,name,utilization.gpu,memory.used",
                "--format=csv,noheader,nounits","--loop-ms=1000"],stdout=self.stream,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        except OSError as error:self.error=type(error).__name__;self.stream.close();self.stream=None

    def close(self):
        if self.result is not None:return self.result
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
        if self.stream:self.stream.close();self.stream=None
        result={"available":False,"requested":self.enabled,"error":self.error,
                "method":"nvidia-smi device-wide samples at 1 Hz, not process attribution or game FPS"}
        if self.path.exists():
            rows=list(csv.reader(self.path.read_text(encoding="utf-8").splitlines()))
            parsed=[]
            for row in rows:
                try:parsed.append((float(row[2]),float(row[3])))
                except (IndexError,ValueError):continue
            result.update(available=bool(parsed),sample_count=len(parsed),gpu_utilization_max_percent=max((r[0] for r in parsed),default=None),
                          memory_used_max_mib=max((r[1] for r in parsed),default=None))
        self.result=result;return result


class ModelWorkload:
    def __init__(self,enabled=False):
        self.stop=Event();self.lock=Lock();self.threads=[];self.samples=deque(maxlen=2048);self.active={};self.error=None
        self.enabled=enabled
        if not enabled:return
        host=urlparse(config.OLLAMA_HOST)
        if host.hostname not in ("127.0.0.1","localhost","::1"):
            self.error="Benchmark contention requires an explicitly configured loopback Ollama";return
        import httpx
        try:
            with httpx.Client(timeout=.8,trust_env=False) as client:
                response=client.get(config.OLLAMA_HOST+"/api/ps");response.raise_for_status()
                self.residency_before=response.json()
        except (httpx.HTTPError,ValueError):self.error="Local Ollama unavailable; contention NOT measured";return
        # Generated fixture only; never a desktop/game screenshot.
        import base64
        from PIL import Image
        image=Image.new("RGB",(320,180),"#303030");buffer=io.BytesIO();image.save(buffer,format="JPEG")
        self.image=base64.b64encode(buffer.getvalue()).decode("ascii")
        for kind in ("qwen","background_vision"):
            thread=Thread(target=self._run,args=(kind,),name="Offline benchmark "+kind,daemon=True)
            self.threads.append(thread);thread.start()

    def _run(self,kind):
        from core.model_client import request_chat
        from core.inference_priority import priority
        from core.cancellation import OperationCancelled
        from contextlib import nullcontext
        class Owner:pass
        owner=Owner()
        while not self.stop.is_set():
            start=time.perf_counter_ns();state="completed"
            try:
                if kind=="qwen":priority.user_started(owner)
                with (priority.background(self.stop) if kind=="background_vision" else nullcontext(self.stop)) as cancellation:
                    with self.lock:self.active[kind]=time.perf_counter_ns()
                    message={"role":"user","content":"Describe the test image briefly." if kind=="background_vision" else "Explain gravity in one short sentence."}
                    if kind=="background_vision":message["images"]=[self.image]
                    request_chat(cancel_event=cancellation,model=config.VISION_MODEL if kind=="background_vision" else config.MODEL,
                        messages=[message],options={"num_predict":32,"num_ctx":8192},keep_alive=config.MODEL_KEEP_ALIVE)
            except OperationCancelled:state="cancelled_or_preempted"
            except Exception as error:
                state=type(error).__name__;self.error="Model workload failed: "+state;self.stop.set()
            finally:
                with self.lock:
                    admitted=self.active.pop(kind,None)
                    self.samples.append({"kind":kind,"request_begin_ns":start,"admitted_ns":admitted,
                                         "end_ns":time.perf_counter_ns(),"outcome":state})
                if kind=="qwen":priority.user_finished(owner)
            self.stop.wait(7 if kind=="qwen" else .5)  # Let the existing six-second grace admit background work.

    def report(self):
        with self.lock:
            return {"requested":self.enabled,"error":self.error,"models":{"text":config.MODEL,"vision":config.VISION_MODEL},
                "samples":list(self.samples),"active":dict(self.active),
                "residency_before":getattr(self,"residency_before",None),
                "method":"Existing request_chat and priority gate; fixed generated fixture. Admission is recorded, not assumed."}

    def close(self):
        self.stop.set()
        for thread in self.threads:thread.join(timeout=3)
        if any(t.is_alive() for t in self.threads):self.error="Model workload shutdown exceeded bound"

"""Resident Kokoro with isolated requests and bounded obsolete-work draining."""

import atexit
import json
import logging
import math
import os
import subprocess
import sys
import time
import tempfile
import uuid
import config
from collections import deque
from contextlib import suppress
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, Condition, current_thread
from contextvars import ContextVar
from contextlib import contextmanager

from core.cancellation import OperationCancelled, check_cancelled
from core.latency import current_trace
from voice.kokoro_worker import ASSET_FILES, CHAMPION_LEXICON, CONVERSATION_LEXICON, validate_assets, validate_request, validate_settings

_priority = ContextVar("capri_tts_priority", default=10)
_WORKER_EXITED=object()  # Internal transport marker; cannot be forged by worker JSON.


@contextmanager
def speech_priority(value):
    token=_priority.set(value)
    try:yield
    finally:_priority.reset(token)


def _record_native_timing(trace, message):
    if trace is None or not isinstance(timing := message.get("timing"), dict):
        return
    fields = {"generate_enter_ns": "native_generate_enter", "callback_ns": "native_first_callback",
              "generate_return_ns": "native_generate_return", "pcm_ns": "native_pcm_ready", "wav_ns": "native_wav_ready"}
    for field, name in fields.items():
        if type(timing.get(field)) is int:
            trace.mark(name, timing[field])
    if type(message.get("received_ns")) is int:
        trace.mark("native_chunk_received", message["received_ns"])
    trace.mark("native_chunk_handled")


class KokoroSession:
    """Serialize synthesis and own the resident worker's complete lifetime."""

    name = "Kokoro"
    worker_filename = "kokoro_worker.py"

    def __init__(self, cancellation_policy=None):
        self._lock = Lock()
        self.process = None
        self._signature = None
        self._messages = None
        self._reader = None
        self._read_stop = Event()
        self._diagnostics = deque(maxlen=8)
        self._options_warning = None
        self.cancellation_policy=cancellation_policy
        self._protocol=1
        self._cancel_callback=False
        self._operation=None
        self._held_message=None
        self._retired=deque(maxlen=32)
        self._closing=Event()
        self._shutdown_lock=Lock()
        self._shutdown_generation=0
        self._drainer=None
        self._condition=Condition()
        self._waiters={}
        self._ticket=0
        self._state="stopped"
        self._starts=0
        self._drained=0
        self._abandoned=0
        self._ready_ns=0
        self._last_drain=None
        self._stale_discarded=0

    def _state_is(self,state):
        with self._condition:
            self._state=state
            if state=="ready":self._ready_ns=time.perf_counter_ns()
            self._condition.notify_all()

    def snapshot(self):
        """Nonblocking numeric/lifecycle diagnostics, no speech or file paths."""
        with self._condition:
            return {"state":self._state,"starts":self._starts,"drained":self._drained,
                    "abandoned":self._abandoned,"pid":getattr(self.process,"pid",None),
                    "waiting":len(self._waiters),"ready_ns":self._ready_ns,
                    "cooperative_supported":self._cancel_callback,"stale_discarded":self._stale_discarded,
                    "request_id":(self._operation or {}).get("id"),
                    "last_drain":dict(self._last_drain) if self._last_drain else None}

    def _acquire(self,cancellation,deadline):
        check_cancelled(cancellation);check_cancelled(self._closing)
        with self._condition:
            if len(self._waiters)>=32:raise RuntimeError("Speech waiting limit reached")
            ticket=self._ticket;self._ticket+=1
            self._waiters[ticket]=_priority.get()
            generation=self._shutdown_generation
        try:
            while True:
                check_cancelled(cancellation);check_cancelled(self._closing)
                if time.monotonic()>=deadline:raise RuntimeError("Speech output is busy. Please try again.")
                with self._condition:
                    if generation!=self._shutdown_generation:raise OperationCancelled()
                    best=min(self._waiters,key=lambda k:(-self._waiters[k],k))
                    if ticket==best and self._lock.acquire(blocking=False):return
                    self._condition.wait(.01)
        finally:
            with self._condition:self._waiters.pop(ticket,None);self._condition.notify_all()

    def _retire_locked(self):
        operation,self._operation=self._operation,None
        if operation:
            if operation.get("id"):self._retired.append(operation["id"])
            if operation.get("directory"):operation["directory"].cleanup()

    def _ready_locked(self,message):
        if message.get("ready") is not True:raise RuntimeError("Kokoro did not finish loading its voice")
        version=message.get("protocol")
        if isinstance(version,bool) or version not in (None,1,2):raise RuntimeError("Unsupported Kokoro worker protocol")
        self._protocol=2 if message.get("protocol")==2 else 1
        self._cancel_callback=message.get("cancel_callback") is True
        self._signature=self._operation["signature"]
        self._operation=None
        self._state_is("ready")

    def _shutdown_locked(self):
        process, self.process = self.process, None
        self._signature = None
        self._state_is("unhealthy")
        self._read_stop.set()
        if process is None:
            self._retire_locked();self._held_message=None;self._state_is("stopped")
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if self._reader is not None:
            self._reader.join(timeout=.2)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        self._reader = None
        self._retire_locked();self._held_message=None
        self._state_is("stopped")

    def shutdown(self):
        with self._shutdown_lock:
            self._closing.set()
            with self._condition:
                self._shutdown_generation+=1;self._condition.notify_all()
            drainer=self._drainer
            try:
                with self._lock:self._shutdown_locked()
                if drainer and drainer is not current_thread():drainer.join(timeout=.2)
            finally:self._closing.clear()

    def _read_messages(self, process, messages, diagnostics, stopped):
        def send(value):
            while not stopped.is_set():
                try:
                    messages.put(value, timeout=.025)
                    return
                except Full:
                    continue
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    diagnostics.append(line.strip()[:500])
                    continue
                if isinstance(value, dict):
                    if isinstance(value.get("timing"), dict):
                        value["received_ns"] = time.perf_counter_ns()
                    send(value)
                if stopped.is_set():
                    break
        finally:
            send({"error": f"{self.name} worker exited. " + " ".join(diagnostics),"_transport_exit":_WORKER_EXITED})

    def _wait(self, cancellation, deadline, on_chunk=None):
        while True:
            check_cancelled(cancellation)
            check_cancelled(self._closing)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Speech generation took too long. Please try again.")
            try:
                if self._held_message is None:self._held_message=self._messages.get(timeout=min(.01,remaining))
                message=self._held_message
            except Empty:
                continue
            check_cancelled(cancellation)
            check_cancelled(self._closing)
            operation=self._operation
            if (self._protocol==2 and operation and operation["kind"]=="synthesis"
                    and message.get("_transport_exit") is not _WORKER_EXITED):
                identifier=message.get("request_id")
                if identifier in self._retired:
                    self._stale_discarded+=1;self._held_message=None;continue
                if identifier!=operation["id"]:raise RuntimeError("Kokoro response request identifier mismatch")
            self._held_message=None
            if "error" in message:
                raise RuntimeError(f"{self.name} failed: {message['error']}")
            if "chunk" in message:
                if on_chunk is None:
                    raise RuntimeError("Unexpected streamed audio from Kokoro")
                on_chunk(message)
                continue
            return message

    def _chunk_locked(self,message,discard=False):
        operation=self._operation
        if self._protocol==2:
            index=operation["index"]
            if type(message.get("chunk_index")) is not int or message["chunk_index"]!=index:
                raise RuntimeError("Kokoro audio chunk order mismatch")
            path=Path(message["chunk"]).resolve()
            native=operation["native_output"]
            if path.parent!=native.parent or path.name not in (native.name,f"{native.stem}-{index}.wav") or not path.is_file():
                raise RuntimeError("Kokoro returned an invalid native audio path")
            operation["index"]+=1
            if discard:path.unlink(missing_ok=True)
            else:
                destination=operation["output"].with_name(f'{operation["output"].stem}-{index}.wav')
                os.replace(path,destination)
                message={**message,"chunk":str(destination)}
        operation["complete"]=message.get("generation_complete") is True
        return message

    def _finish_abandoned_locked(self,deadline):
        operation=self._operation
        result=self._wait(None,deadline,lambda m:self._chunk_locked(m,discard=True))
        with self._condition:
            self._last_drain={"request_id":operation.get("id"),"finished_ns":time.perf_counter_ns(),
                "native_synthesis_cpu_ns":result.get("synthesis_cpu_ns"),
                "native_synthesis_wall_ns":result.get("synthesis_wall_ns")}
        if operation["kind"]=="loading":self._ready_locked(result)
        else:
            if result.get("ok") is not True and result.get("cancelled") is not True:
                raise RuntimeError("Kokoro abandoned synthesis did not complete")
            self._retire_locked();self._state_is("ready")
        self._drained+=1

    def _drain_background(self,deadline):
        # Ownership of the already held primitive Lock transfers to this one
        # bounded thread. No next request can consume the old operation's data.
        try:self._finish_abandoned_locked(deadline)
        except BaseException:self._shutdown_locked()
        finally:
            self._drainer=None
            self._lock.release()
            with self._condition:self._condition.notify_all()

    def _abandon_locked(self,deadline,policy):
        operation=self._operation
        self._abandoned+=1;self._state_is("cancellation_requested")
        duration=(config.KOKORO_STARTUP_DRAIN_SECONDS if operation["kind"]=="loading"
                  else config.KOKORO_ABANDON_TIMEOUT_SECONDS)
        try:
            if operation["kind"]=="synthesis" and policy=="cooperative":
                operation["native_output"].with_name("cancel").touch()
            self._state_is("draining")
            self._drainer=Thread(target=self._drain_background,args=(min(deadline,time.monotonic()+duration),),
                                 name="Capri obsolete Kokoro request",daemon=True)
            self._drainer.start()
            return True
        except Exception:
            self._drainer=None
            self._shutdown_locked()  # Never delete a directory still being written.
            return False

    def _resources(self, asset_dir):
        directory = validate_assets(asset_dir)
        assets = [directory / name for name in ASSET_FILES] + [directory / "espeak-ng-data", CHAMPION_LEXICON, CONVERSATION_LEXICON]
        arguments = [str(directory), str(config.KOKORO_NUM_THREADS)]
        if config.KOKORO_CPU_OPTIONS and str(config.KOKORO_CPU_OPTIONS).casefold() != "none":
            options = Path(config.KOKORO_CPU_OPTIONS).expanduser().resolve()
            if options.is_file():
                assets.append(options)
                arguments.append(str(options))
                self._options_warning = None
            elif self._options_warning != options:
                logging.warning("Kokoro CPU options unavailable; retaining plain CPU")
                self._options_warning = options
        personal = config.PERSONAL_PRONUNCIATIONS_FILE
        if personal and str(personal).casefold() != "none":
            path = Path(personal).expanduser().resolve()
            if path.is_file():
                assets.append(path)
                arguments.extend(("--personal-pronunciations", str(path)))
        return assets, arguments

    def _prepare_locked(self, asset_dir, cancellation, deadline):
        assets, arguments = self._resources(asset_dir)
        signature = (tuple(arguments), *((path.stat().st_mtime_ns, path.stat().st_size) for path in assets))
        if self.process is not None and self.process.poll() is None and signature == self._signature:
            return
        self._shutdown_locked()
        self._state_is("restarting")
        self._operation={"kind":"loading","signature":signature}
        self._protocol=1
        self._messages = Queue(maxsize=8)
        self._read_stop = Event()
        self._diagnostics = deque(maxlen=8)
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).parent / self.worker_filename), *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._starts+=1;self._state_is("loading")
        self._reader = Thread(target=self._read_messages,
                              args=(self.process, self._messages, self._diagnostics, self._read_stop),
                              name=f"Capri {self.name} output", daemon=True)
        self._reader.start()
        ready = self._wait(cancellation, deadline)
        self._ready_locked(ready)
        logging.info("%s speech worker ready", self.name)

    def request(self, asset_dir, cancellation=None, timeout=90, *, text=None, output=None, speaker=53, speed=1.1,
                on_chunk=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 3600 or not math.isfinite(timeout)):
            raise ValueError("Speech timeout must be a finite number between 0 and 3600 seconds")
        speaker, speed = validate_settings(speaker, speed)
        request = None
        trace = current_trace()
        if text is not None:
            text, path, speaker, speed = validate_request(text, output, speaker, speed)
            request = {"text": text, "output": str(path), "speaker": speaker, "speed": speed}
            if on_chunk is not None:
                request["stream"] = True
            if trace is not None:
                request["profile"] = True
        deadline = time.monotonic() + timeout
        policy=self.cancellation_policy or config.KOKORO_CANCEL_POLICY
        if policy not in ("restart","drain","cooperative"):raise ValueError("Invalid Kokoro cancellation policy")
        for duration in (config.KOKORO_STARTUP_DRAIN_SECONDS,config.KOKORO_ABANDON_TIMEOUT_SECONDS):
            if isinstance(duration,bool) or not isinstance(duration,(float,int)) or not 0<duration<=30 or not math.isfinite(duration):
                raise ValueError("Kokoro abandon/startup deadline must be between 0 and 30 seconds")
        self._acquire(cancellation,deadline)
        in_flight = False
        transferred=False
        generation_complete = False
        def chunk_ready(message):
            nonlocal generation_complete
            generation_complete = message.get("generation_complete") is True
            message=self._chunk_locked(message)
            _record_native_timing(trace, message)
            on_chunk(message)
        try:
            check_cancelled(cancellation)
            self._prepare_locked(asset_dir, cancellation, deadline)
            if request is not None:
                check_cancelled(cancellation)
                operation={"kind":"synthesis","complete":False,"index":0,"id":None}
                if self._protocol==2:
                    directory=tempfile.TemporaryDirectory(prefix="capri-native-")
                    # Session shutdown must stop native writers before deletion;
                    # weakref's interpreter-exit cleanup otherwise runs first.
                    directory._finalizer.atexit=False
                    operation.update(id=uuid.uuid4().hex,directory=directory,output=path,
                                     native_output=Path(directory.name)/"audio.wav")
                    request.update(request_id=operation["id"],output=str(operation["native_output"]))
                    if policy=="cooperative" and self._cancel_callback:
                        request["cancel_path"]=str(Path(directory.name)/"cancel")
                self._operation=operation;self._state_is("synthesizing")
                if trace is not None:
                    trace.mark("tts_worker_dispatched")
                in_flight = True
                self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
                self.process.stdin.flush()
                result = self._wait(cancellation, deadline, chunk_ready if on_chunk is not None else None)
                in_flight = False
                if result.get("ok") is not True:
                    raise RuntimeError(f"{self.name} did not confirm speech generation")
                check_cancelled(cancellation)
                if self._protocol==2 and on_chunk is None:os.replace(operation["native_output"],path)
                if trace is not None:
                    _record_native_timing(trace, result)
                    for key in ("synthesis_wall_ns", "synthesis_cpu_ns"):
                        if isinstance(result.get(key), int):
                            trace.collector.record("native_tts_" + key.removesuffix("_ns"), result[key])
                    if type(result.get("first_callback_cpu_ns")) is int:
                        trace.collector.record("native_tts_first_callback_cpu", result["first_callback_cpu_ns"])
        except OperationCancelled:
            # Audio has its own cancellation token and stops immediately. A
            # native chunk can race the final 'ok' by a few milliseconds. Drain
            # that acknowledgment before throwing away a fully loaded model.
            operation=self._operation
            preserve=(policy!="restart" and not self._closing.is_set() and operation is not None
                      and (operation["kind"]=="loading" or in_flight and self._protocol==2))
            if preserve:
                # Fast completed-generation acknowledgment path remains intact.
                if generation_complete:
                    try:
                        self._finish_abandoned_locked(min(deadline,time.monotonic()+config.TTS_CANCEL_DRAIN_SECONDS))
                    except Exception:
                        if self._operation is not None:
                            transferred=self._abandon_locked(deadline,policy)
                else:
                    transferred=self._abandon_locked(deadline,policy)
            elif in_flight:
                try:
                    grace = max(0, min(.1, config.TTS_CANCEL_DRAIN_SECONDS)) if generation_complete else 0
                    result = self._wait(None, min(deadline, time.monotonic() + grace), lambda message: None)
                    if result.get("ok") is not True:
                        self._shutdown_locked()
                except Exception:
                    self._shutdown_locked()
            elif operation is not None and self._signature is None:
                self._shutdown_locked()  # A cancelled model load is incomplete.
            raise
        except BaseException:
            self._shutdown_locked()
            raise
        finally:
            if not transferred:
                self._retire_locked()
                if self._signature is not None:self._state_is("ready")
                self._lock.release()
                with self._condition:self._condition.notify_all()


session = KokoroSession()
atexit.register(session.shutdown)

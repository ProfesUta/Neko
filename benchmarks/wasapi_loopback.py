"""Passive Windows shared-mode loopback onset probe, benchmark use only.

Captures a selected render endpoint, never microphone input or input monitoring.
PCM is inspected in RAM and discarded per packet. No audio is saved. The result
is digital system-mix onset with a WASAPI QPC timestamp, not acoustic output.
"""
import ctypes as C
import os
import time
import uuid
from threading import Event, Lock, Thread

import numpy as np


class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


class WaveFormat(C.Structure):
    _pack_ = 2
    _fields_ = [("tag", C.c_uint16), ("channels", C.c_uint16), ("rate", C.c_uint32),
                ("average", C.c_uint32), ("align", C.c_uint16), ("bits", C.c_uint16), ("extra", C.c_uint16)]


def first_signal(data, channels, bits, floating, threshold=.001):
    """Return the first above-threshold frame, or None. Also used by unit tests."""
    if floating and bits == 32:
        samples = np.frombuffer(data, dtype="<f4").reshape(-1, channels)
    elif not floating and bits in (16, 32):
        samples = np.frombuffer(data, dtype=f"<i{bits//8}").reshape(-1, channels).astype("float64") / (2 ** (bits-1))
    else:
        raise RuntimeError("Loopback probe supports float32 or PCM16/32 mix formats")
    frames = np.flatnonzero(np.any(np.abs(samples) > threshold, axis=1))
    return int(frames[0]) if frames.size else None


def _call(pointer, slot, args, *values):
    table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
    method = C.WINFUNCTYPE(C.c_long, C.c_void_p, *args)(table[slot])
    status = method(pointer, *values)
    if status < 0:
        raise OSError(f"WASAPI HRESULT 0x{status & 0xffffffff:08x}")
    return status


class LoopbackProbe:
    def __init__(self, endpoint_id=None, packet_sink=None, threshold=.001):
        if not 0 < threshold < 1:
            raise ValueError("Loopback signal threshold must be between zero and one")
        self.endpoint_id = endpoint_id
        self.threshold = threshold
        # Explicit diagnostics only. Normal operation never imports this module.
        # A sink must be bounded/nonblocking; it runs on the capture thread.
        self.packet_sink = packet_sink
        self.ready, self.stop, self.found = Event(), Event(), Event()
        self.lock = Lock()
        self.error = self.onset = self.received = self.armed = None
        self._sample_error = None
        self.last_signal = 0
        self.invalid_timestamps = self.discontinuities = 0
        self.format = None
        self.thread = Thread(target=self._run, name="Explicit WASAPI benchmark", daemon=True)

    def __enter__(self):
        if os.name != "nt":
            raise RuntimeError("WASAPI loopback is Windows-only")
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            self.__exit__()
            raise RuntimeError(self.error or "Loopback initialization timed out")
        return self

    def arm(self):
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            now = time.perf_counter_ns()
            if now - self.last_signal < 200_000_000:
                raise RuntimeError("Output mix is not quiet; loopback onset would be ambiguous")
            self.found.clear()
            self.onset = self.received = None
            self._sample_error = None
            self.armed = now

    def result(self, timeout=1):
        self.found.wait(timeout)
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            if self._sample_error:
                raise RuntimeError(self._sample_error)
            return (self.onset, self.received) if self.onset is not None else None

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(2)
        if self.thread.is_alive():
            raise RuntimeError("Loopback capture did not stop")

    def _run(self):
        pointers, mix, initialized, started = [], C.c_void_p(), False, False
        client = None
        ole = C.OleDLL("ole32")
        # These COM cleanup functions return void, not OleDLL's default HRESULT.
        ole.CoTaskMemFree.argtypes = (C.c_void_p,)
        ole.CoTaskMemFree.restype = None
        ole.CoUninitialize.argtypes = ()
        ole.CoUninitialize.restype = None
        try:
            ole.CoInitializeEx(None, 0)
            initialized = True
            enumerator, endpoint = C.c_void_p(), C.c_void_p()
            clsid = GUID("bcde0395-e52f-467c-8e3d-c4579291692e")
            iid = GUID("a95664d2-9614-4f35-a746-de8db63617e6")
            ole.CoCreateInstance(C.byref(clsid), None, 23, C.byref(iid), C.byref(enumerator))
            pointers.append(enumerator)
            if self.endpoint_id:
                _call(enumerator, 5, (C.c_wchar_p, C.POINTER(C.c_void_p)), self.endpoint_id, C.byref(endpoint))
            else:
                _call(enumerator, 4, (C.c_int, C.c_int, C.POINTER(C.c_void_p)), 0, 0, C.byref(endpoint))
            pointers.append(endpoint)
            client = C.c_void_p()
            iid = GUID("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")
            _call(endpoint, 3, (C.POINTER(GUID), C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)), C.byref(iid), 23, None, C.byref(client))
            pointers.append(client)
            _call(client, 8, (C.POINTER(C.c_void_p),), C.byref(mix))
            fmt = C.cast(mix, C.POINTER(WaveFormat)).contents
            if (not 1 <= fmt.channels <= 32 or not 8000 <= fmt.rate <= 384000
                    or fmt.tag == 65534 and fmt.extra < 22):
                raise RuntimeError("Unsupported loopback mix format")
            floating = fmt.tag == 3 or (fmt.tag == 65534 and C.c_uint32.from_address(mix.value + 24).value == 3)
            if (fmt.tag not in (1, 3, 65534) or fmt.align != fmt.channels * fmt.bits // 8
                    or fmt.tag == 65534 and C.c_uint32.from_address(mix.value + 24).value not in (1, 3)):
                raise RuntimeError("Unsupported loopback mix layout")
            first_signal(bytes(fmt.align), fmt.channels, fmt.bits, floating)  # Validate before starting.
            self.format = {"rate": fmt.rate, "channels": fmt.channels, "bits": fmt.bits, "floating": floating}
            _call(client, 3, (C.c_int, C.c_uint32, C.c_int64, C.c_int64, C.c_void_p, C.c_void_p),
                  0, 0x20000, 1_000_000, 0, mix, None)
            capture = C.c_void_p()
            iid = GUID("c8adbd64-e71e-48a0-a4de-185c395cd317")
            _call(client, 14, (C.POINTER(GUID), C.POINTER(C.c_void_p)), C.byref(iid), C.byref(capture))
            pointers.append(capture)
            _call(client, 10, ())
            started = True
            self.ready.set()
            while not self.stop.wait(.005):
                available = C.c_uint32()
                _call(capture, 5, (C.POINTER(C.c_uint32),), C.byref(available))
                while available.value:
                    data, frames, flags, position, qpc = C.c_void_p(), C.c_uint32(), C.c_uint32(), C.c_uint64(), C.c_uint64()
                    _call(capture, 3, (C.POINTER(C.c_void_p), C.POINTER(C.c_uint32), C.POINTER(C.c_uint32),
                                      C.POINTER(C.c_uint64), C.POINTER(C.c_uint64)),
                          C.byref(data), C.byref(frames), C.byref(flags), C.byref(position), C.byref(qpc))
                    try:
                        received = time.perf_counter_ns()
                        raw = (bytes(frames.value * fmt.align) if self.packet_sink is not None else b"") if flags.value & 2 else C.string_at(data, frames.value * fmt.align)
                        if self.packet_sink is not None:
                            self.packet_sink(raw, frames.value, qpc.value * 100, flags.value, self.format)
                        index = None if flags.value & 2 else first_signal(raw, fmt.channels, fmt.bits, floating, self.threshold)
                        with self.lock:
                            self.discontinuities += bool(flags.value & 1)
                            self.invalid_timestamps += bool(flags.value & 4)
                            if flags.value & 4 and self.armed is not None and self.onset is None:
                                self._sample_error = "Loopback timestamp invalid; sample rejected"
                                self.found.set()
                            if index is not None:
                                self.last_signal = received
                                onset = qpc.value * 100 + int(index * 1e9 / fmt.rate)
                                if self.armed is not None and self.onset is None and onset >= self.armed and not flags.value & 4:
                                    self.onset, self.received = onset, received
                                    self.found.set()
                    finally:
                        _call(capture, 4, (C.c_uint32,), frames.value)
                    _call(capture, 5, (C.POINTER(C.c_uint32),), C.byref(available))
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.ready.set()
            self.found.set()
        finally:
            if started:
                try:
                    _call(client, 11, ())
                except OSError:
                    pass
            if mix.value:
                ole.CoTaskMemFree(mix)
            for pointer in reversed(pointers):
                _call(pointer, 2, ())
            if initialized:
                ole.CoUninitialize()

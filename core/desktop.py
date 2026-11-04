"""Windows foreground-monitor tracking and physical-pixel overlay rectangles."""

import ctypes
import os
from ctypes import wintypes
from threading import RLock


class MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
    ]


def _rect_tuple(rect):
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


class DesktopTracker:
    def __init__(self):
        self._lock = RLock()
        self._last_external = None
        self._user32 = None
        self._dwm = None
        if os.name == "nt":
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            api = self._user32
            api.GetForegroundWindow.restype = wintypes.HWND
            api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            api.MonitorFromWindow.restype = wintypes.HANDLE
            api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            api.IsWindow.argtypes = [wintypes.HWND]
            self._dwm = ctypes.WinDLL("dwmapi")
            self._dwm.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ]

    def _target(self):
        if self._user32 is None:
            return None
        window = self._user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if window and pid.value != os.getpid():
            self._last_external = window
        target = self._last_external
        return target if target and self._user32.IsWindow(target) else None

    def _source(self, window):
        if not window:
            return "Desktop monitor"
        title = ctypes.create_unicode_buffer(1024)
        self._user32.GetWindowTextW(window, title, len(title))
        return title.value[:300] or "Active application"

    def current_source(self):
        with self._lock:
            return str(self._target() or "desktop")

    def capture_geometry(self, fallback, overlays, handles):
        with self._lock:
            target = self._target()
            source = self._source(target)
            region = fallback
            monitor_window = target or (handles[0] if handles else None)
            if monitor_window and self._user32 is not None:
                monitor = self._user32.MonitorFromWindow(monitor_window, 2)
                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                if monitor and self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    region = _rect_tuple(info.rcMonitor)
            physical_overlays = list(overlays)
            if self._dwm is not None:
                for index, handle in enumerate(handles):
                    if index >= len(physical_overlays):
                        break
                    rect = wintypes.RECT()
                    result = self._dwm.DwmGetWindowAttribute(
                        handle, 9, ctypes.byref(rect), ctypes.sizeof(rect),
                    )
                    if result == 0:
                        physical_overlays[index] = _rect_tuple(rect)
            return region, physical_overlays, source, str(target or "desktop")

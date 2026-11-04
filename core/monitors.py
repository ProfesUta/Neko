"""GUI-owned monitor discovery with immutable, worker-safe coordinate snapshots."""

import ctypes
import os
from ctypes import wintypes
from threading import RLock

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication

from core.spatial_geometry import Monitor, Rect


class MonitorInfoEx(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                ("work", wintypes.RECT), ("flags", wintypes.DWORD), ("device", wintypes.WCHAR * 32)]


def native_monitors():
    if os.name != "nt":
        return {}
    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfoEx)]
    api.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, callback_type, wintypes.LPARAM]
    result = {}
    @callback_type
    def visit(handle, dc, rect, data):
        info = MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if api.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.monitor
            result[info.device] = Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)
        return True
    api.EnumDisplayMonitors(None, None, visit, 0)
    return result


def _qt_rect(rect):
    return Rect(rect.x(), rect.y(), rect.width(), rect.height())


def match_native_monitor(name, logical, scale, native):
    """QScreen.name may be a friendly model name rather than a native device ID."""
    if name in native:
        return name, native[name]
    matches = [(device, physical) for device, physical in native.items()
               if abs(physical.x - logical.x) <= 1 and abs(physical.y - logical.y) <= 1
               and abs(physical.width - logical.width * scale) <= 2
               and abs(physical.height - logical.height * scale) <= 2]
    return matches[0] if len(matches) == 1 else (None, None)


class MonitorManager(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = RLock()
        self._monitors = ()
        self.revision = 0
        self._disposed = False
        self._screens = set()
        self._app = QGuiApplication.instance()
        self._app.screenAdded.connect(self.refresh)
        self._app.screenRemoved.connect(self.refresh)
        self._app.primaryScreenChanged.connect(self.refresh)
        self.refresh()

    def snapshot(self):
        with self._lock:
            return self._monitors

    def resolve(self, reference=None):
        monitors = self.snapshot()
        if reference is None or reference == "":
            return None
        matches = []
        for monitor in monitors:
            if str(reference).casefold() in (monitor.id.casefold(), str(monitor.number),
                                             f"monitor {monitor.number}", monitor.name.casefold()):
                matches.append(monitor)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("That monitor name is ambiguous. Use its unique ID or number.")
        raise ValueError(f"Monitor {reference!r} is not connected. Use get_monitors for current IDs.")

    def refresh(self, *args):
        if self._disposed:
            return
        screens = set(self._app.screens())
        signals = ("geometryChanged", "availableGeometryChanged", "logicalDotsPerInchChanged",
                   "physicalDotsPerInchChanged")
        for screen in self._screens - screens:
            for name in signals:
                try:
                    getattr(screen, name).disconnect(self.refresh)
                except (RuntimeError, TypeError):
                    pass
        for screen in screens - self._screens:
            for name in signals:
                getattr(screen, name).connect(self.refresh)
        self._screens = screens
        native = native_monitors() if QGuiApplication.platformName() == "windows" else {}
        primary = self._app.primaryScreen()
        ordered = sorted(screens, key=lambda screen: (screen != primary, screen.name(),
                                                     screen.geometry().x(), screen.geometry().y()))
        monitors = []
        used_ids = set()
        for number, screen in enumerate(ordered, 1):
            logical = _qt_rect(screen.geometry())
            scale = screen.devicePixelRatio()
            device_id, physical = match_native_monitor(screen.name(), logical, scale, native)
            supported = physical is not None or scale == 1
            physical = physical or logical
            identity = device_id or f"{screen.name() or 'display'}:{screen.serialNumber() or number}"
            if identity in used_ids:
                identity += f"@{logical.x},{logical.y}:{number}"
            used_ids.add(identity)
            monitors.append(Monitor(
                identity,
                number, screen.name() or f"Monitor {number}",
                logical, physical, _qt_rect(screen.availableGeometry()), scale,
                screen == primary, supported,
            ))
        with self._lock:
            changed = tuple(monitors) != self._monitors
            self._monitors = tuple(monitors)
            if changed:
                self.revision += 1
        if changed:
            self.changed.emit()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        for signal in (self._app.screenAdded, self._app.screenRemoved, self._app.primaryScreenChanged):
            signal.disconnect(self.refresh)
        for screen in self._screens:
            for name in ("geometryChanged", "availableGeometryChanged", "logicalDotsPerInchChanged",
                         "physicalDotsPerInchChanged"):
                try:
                    getattr(screen, name).disconnect(self.refresh)
                except (RuntimeError, TypeError):
                    pass
        self._screens.clear()

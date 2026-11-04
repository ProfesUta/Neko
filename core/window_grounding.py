"""Read-only Windows window/accessibility grounding. Never clicks or activates apps."""

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from core.cancellation import check_cancelled
from core.spatial_geometry import Rect


@dataclass(frozen=True)
class VisibleWindow:
    handle: int
    pid: int
    title: str
    application: str
    bounds: Rect  # Always physical global pixels.
    z_order: int = 0
    is_desktop: bool = False

    def to_dict(self):
        return {"window_id": str(self.handle), "title": self.title, "application": self.application,
                "bounds_physical": self.bounds.to_dict(), "pid": self.pid,
                "kind": "desktop" if self.is_desktop else "window"}


def visible_parts(rect, window, windows):
    parts = [rect.intersection(window.bounds)]
    for above in windows:
        if above.z_order >= window.z_order:
            continue
        parts = [part for candidate in parts for part in candidate.subtract(above.bounds)]
        parts = sorted(parts, key=lambda part: part.area, reverse=True)[:32]
    return [part for part in parts if part.area >= 16]


# IsOffscreen alone does not mean unobscured: Python also checks Z-order occlusion.
# Only Name/geometry/identity are read; Value/Text/password contents are never read.
UIA_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$timer = [Diagnostics.Stopwatch]::StartNew()
$results = [Collections.Generic.List[object]]::new()
$count = 0
$found = $false
foreach ($handle in $payload.handles) {
  if ($found -or $timer.ElapsedMilliseconds -gt 2500 -or $count -ge 1800) { break }
  try { $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$handle) }
  catch { continue }
  $queue = [Collections.Generic.Queue[System.Windows.Automation.AutomationElement]]::new()
  $queue.Enqueue($root)
  while ($queue.Count -gt 0 -and $count -lt 1800 -and $timer.ElapsedMilliseconds -lt 2500) {
    $node = $queue.Dequeue()
    $count++
    try {
      $current = $node.Current
      if (-not $current.IsOffscreen -and -not $current.IsPassword) {
        $name = $current.Name
        $rect = $current.BoundingRectangle
        $runtime = $node.GetRuntimeId() -join '.'
        if ($name -and $rect.Width -gt 1 -and $rect.Height -gt 1 -and $results.Count -lt 600) {
          if (-not $payload.runtime_id -or $runtime -eq $payload.runtime_id) {
            $results.Add(@{name=$name.Substring(0, [Math]::Min(180,$name.Length));
              x=$rect.X; y=$rect.Y; width=$rect.Width; height=$rect.Height;
              handle=[string]$handle; runtime_id=$runtime; observed_ms=$timer.ElapsedMilliseconds;
              control_type=$current.ControlType.ProgrammaticName; automation_id=$current.AutomationId})
            if ($payload.runtime_id) { $found = $true; break }
          }
        }
      }
      $child = $walker.GetFirstChild($node)
      while ($null -ne $child -and $queue.Count -lt 1800 -and $timer.ElapsedMilliseconds -lt 2500) {
        $queue.Enqueue($child)
        $child = $walker.GetNextSibling($child)
      }
    } catch { continue }
  }
}
foreach ($item in $results) { $item.age_ms = $timer.ElapsedMilliseconds - $item.observed_ms }
ConvertTo-Json -InputObject @($results.ToArray()) -Depth 4 -Compress
"""


class WindowGrounder:
    def __init__(self):
        self.supported = os.name == "nt"
        self._api = self._dwm = None
        self.last_error = ""
        if self.supported:
            self._api = ctypes.WinDLL("user32", use_last_error=True)
            self._dwm = ctypes.WinDLL("dwmapi")
            api = self._api
            api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            api.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
            api.FindWindowExW.restype = wintypes.HWND
            api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            api.IsWindowVisible.argtypes = api.IsIconic.argtypes = api.IsWindow.argtypes = [wintypes.HWND]
            self._dwm.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ]

    def _read(self, handle, z_order=0):
        api = self._api
        if not api or not api.IsWindow(handle) or not api.IsWindowVisible(handle) or api.IsIconic(handle):
            return None
        pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if pid.value == os.getpid():
            return None
        cloaked = wintypes.DWORD()
        if self._dwm.DwmGetWindowAttribute(handle, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return None
        rect = wintypes.RECT()
        if self._dwm.DwmGetWindowAttribute(handle, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            if not api.GetWindowRect(handle, ctypes.byref(rect)):
                return None
        bounds = Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        if bounds.area < 16:
            return None
        title = ctypes.create_unicode_buffer(512)
        api.GetWindowTextW(handle, title, len(title))
        klass = ctypes.create_unicode_buffer(256)
        api.GetClassNameW(handle, klass, len(klass))
        is_desktop = klass.value in ("Progman", "WorkerW")
        if is_desktop and not api.FindWindowExW(handle, None, "SHELLDLL_DefView", None):
            return None  # Empty wallpaper hosts must not occlude real desktop icons.
        import psutil
        try:
            application = psutil.Process(pid.value).name()
        except (psutil.Error, OSError):
            application = klass.value
        return VisibleWindow(int(handle), pid.value, title.value or ("Desktop" if is_desktop else ""),
                             application, bounds, z_order, is_desktop)

    def windows(self, cancel_event=None):
        if not self.supported:
            return []
        check_cancelled(cancel_event)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        result = []
        @callback_type
        def visit(handle, data):
            if len(result) >= 256 or (cancel_event and cancel_event.is_set()):
                return False
            window = self._read(handle, len(result))
            if window is not None:
                result.append(window)
            return True
        self._api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        enumerated = self._api.EnumWindows(visit, 0)
        self.last_error = "" if enumerated or result else "Native window enumeration is unavailable in this desktop session."
        check_cancelled(cancel_event)
        return result

    def unchanged(self, handle, pid, bounds):
        """Small native guard used immediately before drawing; no tree walk/capture."""
        window = self._read(handle)
        return window is not None and window.pid == pid and window.bounds == bounds

    def elements(self, windows, cancel_event=None, runtime_id=""):
        if not self.supported or not windows:
            return []
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not executable.is_file():
            return []
        payload = json.dumps({"handles": [str(window.handle) for window in windows[:12]], "runtime_id": runtime_id})
        with subprocess.Popen(
            [str(executable), "-NoProfile", "-NonInteractive", "-STA", "-Command", UIA_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as process:
            deadline = time.monotonic() + 6
            first = True
            try:
                while True:
                    check_cancelled(cancel_event)
                    if time.monotonic() > deadline:
                        return []
                    try:
                        output, _error = process.communicate(payload.encode("utf-8") if first else None, timeout=0.05)
                        if process.returncode:
                            return []
                        elements = json.loads(output.decode("utf-8-sig") or "[]")
                        for item in elements:
                            item["observed_at"] = time.monotonic() - max(0, item.get("age_ms", 0)) / 1000
                        return elements
                    except subprocess.TimeoutExpired:
                        first = False
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

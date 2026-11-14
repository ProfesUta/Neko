"""Cropped visible game-client pixels using the existing MSS/Win32 stack.

Physical client coordinates are resolved on the capture thread with per-monitor
DPI awareness. No screen is read unless the game is foreground and unobscured.
No frames are encoded, saved, uploaded or sent to the conversation model.
"""
import ctypes
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from ctypes import wintypes

from core.window_grounding import WindowGrounder
import config


@dataclass(frozen=True)
class RegionFrame:
    regions: dict
    captured_at: float
    sequence: int
    source_id: str
    context_key: tuple
    capture_started_ns: int = 0
    ready_ns: int = 0

    @property
    def byte_size(self):
        return sum(image.nbytes for image in self.regions.values())


def physical_regions(client,rois):
    x,y,w,h=client
    if w<=0 or h<=0:raise ValueError("The game client has no visible area")
    return {name:{"left":x+round(rx*w),"top":y+round(ry*h),
                  "width":round((rx+rw)*w)-round(rx*w),"height":round((ry+rh)*h)-round(ry*h)}
            for name,(rx,ry,rw,rh) in rois.items()}


def intersects(a,b):
    return a[0]<b[0]+b[2] and b[0]<a[0]+a[2] and a[1]<b[1]+b[3] and b[1]<a[1]+a[3]


class GameWindowCapture:
    def __init__(self,profile,allowed,grounder=None):
        self.profile,self.allowed=profile,allowed
        self.grounder=grounder or WindowGrounder()
        self.handle=None
        self._scan_at=0.
        self.sequence=0
        self.api=ctypes.WinDLL("user32",use_last_error=True) if os.name=="nt" else None
        if self.api is not None:
            self.api.GetForegroundWindow.restype=wintypes.HWND
            self.api.GetWindow.argtypes=[wintypes.HWND,wintypes.UINT]
            self.api.GetWindow.restype=wintypes.HWND
            self.api.GetClientRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
            self.api.ClientToScreen.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.POINT)]
            self.api.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
            self.api.IsWindowVisible.argtypes=[wintypes.HWND]
            self.api.IsIconic.argtypes=[wintypes.HWND]
            self.api.IsWindow.argtypes=[wintypes.HWND]
            self.api.SetThreadDpiAwarenessContext.argtypes=[wintypes.HANDLE]
            self.api.SetThreadDpiAwarenessContext.restype=wintypes.HANDLE

    @contextmanager
    def physical_context(self):
        if self.api is None:raise RuntimeError("Game-window capture requires Windows")
        old=self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not old:raise RuntimeError("Per-monitor physical coordinate mapping is unavailable")
        try:yield
        finally:self.api.SetThreadDpiAwarenessContext(old)

    def geometry(self):
        if not self.allowed():raise RuntimeError("Local game capture is not approved/enabled for this session")
        now=time.monotonic()
        if self.handle is None or not self.api.IsWindow(self.handle):
            if now<self._scan_at:raise RuntimeError("No visible League game window")
            self._scan_at=now+1
            windows=[window for window in self.grounder.windows() if window.application.lower().replace(" ","")=="leagueoflegends.exe"]
            foreground=self.api.GetForegroundWindow()
            found=next((window for window in windows if window.handle==foreground),None)
            if found is None:raise RuntimeError("League must be the foreground game window")
            self.handle=found.handle
        handle=self.handle
        if (self.api.GetForegroundWindow()!=handle or self.api.IsIconic(handle) or not self.api.IsWindowVisible(handle)):
            raise RuntimeError("Game capture paused while League is minimized or not foreground")
        rect=wintypes.RECT();origin=wintypes.POINT(0,0)
        if not self.api.GetClientRect(handle,ctypes.byref(rect)) or not self.api.ClientToScreen(handle,ctypes.byref(origin)):
            raise RuntimeError("Could not resolve the physical game client rectangle")
        self.profile.validate_size(rect.right,rect.bottom)
        client=(origin.x,origin.y,rect.right,rect.bottom)
        regions=physical_regions(client,self.profile.rois())
        # MSS reads visible desktop pixels: reject occlusion rather than capture
        # another application's contents or mistake overlays for game evidence.
        above=self.api.GetWindow(handle,3)  # GW_HWNDPREV
        count=0
        while above and count<128:
            count+=1
            if self.api.IsWindowVisible(above) and not self.api.IsIconic(above):
                box=wintypes.RECT()
                if not self.api.GetWindowRect(above,ctypes.byref(box)):
                    raise RuntimeError("Could not verify game-window visibility")
                other=(box.left,box.top,box.right-box.left,box.bottom-box.top)
                if any(intersects(other,(r["left"],r["top"],r["width"],r["height"])) for r in regions.values()):
                    raise RuntimeError("Game regions are obscured; local detection paused")
            above=self.api.GetWindow(above,3)
        if above:raise RuntimeError("Window occlusion verification exceeded its bound")
        return regions,client

    @contextmanager
    def open(self):
        import mss
        import cv2
        import numpy as np
        with self.physical_context(),mss.mss() as capture:
            def grab():
                regions,client=self.geometry()
                started=time.perf_counter_ns() if config.LEAGUE_LATENCY_METRICS_ENABLED else 0
                at=time.monotonic()
                images={}
                for name,region in regions.items():
                    images[name]=np.asarray(capture.grab(region))[:,:,:3].copy()
                    if name=="viewport" and images[name].shape[1]>960:
                        factor=960/images[name].shape[1]
                        images[name]=cv2.resize(images[name],None,fx=factor,fy=factor,interpolation=cv2.INTER_AREA)
                    images[name].setflags(write=False)
                after_regions,after_client=self.geometry()
                if after_client!=client or after_regions!=regions or not self.allowed():
                    raise RuntimeError("Game context changed during capture; frame discarded")
                self.sequence+=1
                ready=time.perf_counter_ns() if started else 0
                return RegionFrame(images,at,self.sequence,str(self.handle),(client,),started,ready)
            yield grab

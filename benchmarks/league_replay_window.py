"""Own-process, visibly labelled OFFLINE replay capture. Never accepts a game HWND.

Reuses GameWindowCapture's MSS/physical-DPI acquisition. This private benchmark
adapter cannot be selected by the live League worker or its approval gate.
"""
import ctypes
import os
import uuid
import time
from ctypes import wintypes
from dataclasses import replace

from PySide6.QtCore import Qt,QRect
from PySide6.QtGui import QImage,QPainter,QColor
from PySide6.QtWidgets import QWidget,QApplication

from core.league_capture import GameWindowCapture,physical_regions,intersects


class ReplayWindow(QWidget):
    def __init__(self,profile):
        super().__init__()
        self.profile=profile;self.token=uuid.uuid4().hex;self.sequence=0;self.video_s=None;self.image=None
        self.setWindowTitle("OFFLINE Neko replay benchmark "+self.token)
        self.setWindowFlags(Qt.WindowType.Window|Qt.WindowType.FramelessWindowHint)
        screen=QApplication.primaryScreen();self.setGeometry(screen.geometry())
        self.showFullScreen();self.raise_();self.activateWindow();QApplication.processEvents()
        self.capture_adapter=ReplayWindowCapture(profile,self)
        if self.capture_adapter.api:
            # Explicitly launched visible benchmark only. Never activates a game
            # or another application's window. Windows may still deny activation.
            api=self.capture_adapter.api;handle=self.capture_adapter.handle
            api.SetForegroundWindow.argtypes=[wintypes.HWND]
            api.SetForegroundWindow(handle)
            until=time.perf_counter()+1
            while api.GetForegroundWindow()!=handle and time.perf_counter()<until:
                QApplication.processEvents();time.sleep(.01)
        self._context=self.capture_adapter.open();self._grab=self._context.__enter__()

    def present(self,image,video_s,sequence):
        if not self.isVisible():raise RuntimeError("Offline replay window was closed")
        h,w=image.shape[:2]
        self.image=QImage(image.data,w,h,image.strides[0],QImage.Format.Format_BGR888).copy()
        self.sequence=sequence+1;self.video_s=video_s;self.repaint()

    def image_rect(self):
        ratio=min(self.width()/self.profile.reference[0],self.height()/self.profile.reference[1])
        w=round(self.profile.reference[0]*ratio);h=round(self.profile.reference[1]*ratio)
        return QRect((self.width()-w)//2,(self.height()-h)//2,w,h)

    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),QColor("black"))
        if self.image:painter.drawImage(self.image_rect(),self.image)
        painter.fillRect(0,0,600,40,QColor("#210009"));painter.setPen(QColor("#ff6688"))
        painter.drawText(275,25,"OFFLINE REPLAY - SILENT VIDEO")
        # A frame marker outside the calibrated minimap associates captured
        # screen pixels with the presented source sample. It is not a DWM clock.
        for bit in range(24):
            painter.fillRect(4+bit*10,8,8,16,QColor("white" if self.sequence&(1<<bit) else "black"))

    def capture(self):
        if not self.isVisible():raise RuntimeError("Offline replay window is not visible")
        def marker(image):
            h,w=image.shape[:2]
            return sum((1<<bit) for bit in range(24) if float(image[h//2,min(w-1,round((bit+.5)*w/24))].mean())>127)
        started=time.perf_counter_ns();deadline=started+40_000_000
        # Repaint returning does not mean DWM has presented the frame. Bounded
        # marker validation waits for actual pixels; all waits remain in T1/T2.
        while True:
            self.capture_adapter.marker_only=True;before=self._grab()
            self.capture_adapter.marker_only=False;frame=self._grab()
            if marker(before.regions["marker"])==self.sequence and marker(frame.regions["marker"])==self.sequence:
                return replace(frame,regions={k:v for k,v in frame.regions.items() if k!="marker"},capture_started_ns=started)
            if time.perf_counter_ns()>=deadline:
                raise RuntimeError("Replay compositor/source marker mismatch; capture rejected")
            QApplication.processEvents();time.sleep(.002)

    def dispose(self):
        try:self._context.__exit__(None,None,None)
        finally:self.close();self.deleteLater();QApplication.processEvents()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Escape:self.close()
        else:super().keyPressEvent(event)


class ReplayWindowCapture(GameWindowCapture):
    def __init__(self,profile,window):
        self.window=window;self.marker_only=False;self.owner_pid=os.getpid();self.token=window.token
        super().__init__(profile,lambda:window.isVisible() and window.token==self.token,grounder=object())
        self.handle=int(window.winId())
        if self.api:
            self.api.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
            self.api.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]

    def geometry(self):
        if self.api is None:raise RuntimeError("Replay-window capture requires Windows")
        pid=wintypes.DWORD();self.api.GetWindowThreadProcessId(self.handle,ctypes.byref(pid))
        title=ctypes.create_unicode_buffer(256);self.api.GetWindowTextW(self.handle,title,256)
        if (pid.value!=self.owner_pid or title.value!="OFFLINE Neko replay benchmark "+self.token or not self.allowed()
                or not self.api.IsWindow(self.handle) or self.api.IsIconic(self.handle)
                or self.api.GetForegroundWindow()!=self.handle):
            raise RuntimeError("Only this foreground benchmark's own replay window may be captured (ownership/title/visibility/foreground check failed)")
        rect=wintypes.RECT();origin=wintypes.POINT(0,0)
        if not self.api.GetClientRect(self.handle,ctypes.byref(rect)) or not self.api.ClientToScreen(self.handle,ctypes.byref(origin)):
            raise RuntimeError("Cannot resolve replay-window physical geometry")
        sx,sy=rect.right/self.window.width(),rect.bottom/self.window.height();content=self.window.image_rect()
        client=(origin.x+round(content.x()*sx),origin.y+round(content.y()*sy),round(content.width()*sx),round(content.height()*sy))
        self.profile.validate_size(client[2],client[3]);regions={} if self.marker_only else physical_regions(client,self.profile.rois())
        regions["marker"]={"left":origin.x+round(4*sx),"top":origin.y+round(8*sy),"width":round(240*sx),"height":round(16*sy)}
        above=self.api.GetWindow(self.handle,3);count=0
        while above and count<128:
            count+=1
            if self.api.IsWindowVisible(above) and not self.api.IsIconic(above):
                box=wintypes.RECT()
                if not self.api.GetWindowRect(above,ctypes.byref(box)):raise RuntimeError("Unverified replay occlusion")
                other=(box.left,box.top,box.right-box.left,box.bottom-box.top)
                if any(intersects(other,(r["left"],r["top"],r["width"],r["height"])) for r in regions.values()):
                    raise RuntimeError("Replay regions are covered; capture rejected")
            above=self.api.GetWindow(above,3)
        if above:raise RuntimeError("Replay occlusion bound exceeded")
        return regions,client

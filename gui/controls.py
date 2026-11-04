"""Transient character controls, with native outside-click dismissal."""

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget,
)


class ControlsPopup(QFrame):
    action_requested = Signal(str)
    geometry_changed = Signal()
    owner_right_clicked = Signal(QPoint)

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("controlsPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.mode = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self.controls = QWidget(self)
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.voice_button = self._button("MIC OFF", "voiceButton", "voice", checkable=True)
        self.voice_button.setAccessibleName("Toggle microphone")
        self.voice_button.setToolTip("Toggle microphone (Ctrl+M)")
        self.vision_button = self._button("VISION OFF", "visionButton", "vision", checkable=True)
        self.vision_button.setAccessibleName("Toggle local screen perception")
        self.vision_button.setToolTip("Toggle screen awareness. Images stay local and are not saved.")
        row.addWidget(self.voice_button)
        row.addWidget(self.vision_button)
        controls_layout.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.chat_button = self._button("CHAT", "chatButton", "chat")
        self.chat_button.setToolTip("Open conversation (Ctrl+J)")
        self.stop_button = self._button("STOP", "stopButton", "stop")
        self.stop_button.setToolTip("Interrupt the current conversation (Ctrl+.)")
        self.stop_button.setEnabled(False)
        row.addWidget(self.chat_button)
        row.addWidget(self.stop_button)
        controls_layout.addLayout(row)
        self.advice_button = self._button("ADVICE ON", "adviceButton", "advice", checkable=True)
        self.advice_button.setAccessibleName("Toggle urgent screen advice")
        self.advice_button.setToolTip("Speak up only when visible information needs your attention now.")
        controls_layout.addWidget(self.advice_button)
        layout.addWidget(self.controls)

        self.exit_button = self._button("EXIT", "closeButton", "exit")
        self.exit_button.setAccessibleName("Exit Capri")
        layout.addWidget(self.exit_button)

    def _button(self, text, name, action, checkable=False):
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.setCheckable(checkable)
        button.clicked.connect(lambda checked=False: self._activate(action))
        return button

    def _activate(self, action):
        self.hide()
        self.action_requested.emit(action)

    def show_at(self, mode, position):
        self.mode = mode
        self.controls.setVisible(mode == "controls")
        self.exit_button.setVisible(mode == "exit")
        self.layout().activate()
        self.setFixedSize(306 if mode == "controls" else 140, self.layout().sizeHint().height())
        screen = QGuiApplication.screenAt(position) or self.parentWidget().screen()
        position = position + QPoint(12, 12)
        if screen is not None:
            bounds = screen.availableGeometry()
            position.setX(max(bounds.left(), min(position.x(), bounds.right() - self.width() + 1)))
            position.setY(max(bounds.top(), min(position.y(), bounds.bottom() - self.height() + 1)))
        self.move(position)
        self.show()
        self.raise_()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ApplicationDeactivate:
            self.hide()
        elif event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
            owner = self.parentWidget()
            in_popup = watched is self or self.isAncestorOf(watched)
            in_character = watched.window() is owner
            if not in_popup and not in_character:
                # Covers other Capri windows; Qt.Popup handles other applications.
                self.hide()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if not self.rect().contains(event.position().toPoint()):
            # Qt clears this flag on each new mouse event, so set it at dismissal.
            # Replaying an outside click on Capri would reopen the controls.
            position = event.globalPosition().toPoint()
            on_owner = self.parentWidget().frameGeometry().contains(position)
            self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, on_owner)
            right_on_owner = (
                event.button() == Qt.MouseButton.RightButton
                and on_owner
            )
            self.hide()
            if right_on_owner:
                self.owner_right_clicked.emit(position)
            event.accept()
            return
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            # Clicks on unused space (or a disabled control) also dismiss it.
            self.hide()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QApplication.instance().installEventFilter(self)
        self.geometry_changed.emit()

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)
        self.geometry_changed.emit()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.geometry_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.geometry_changed.emit()

"""A brief screen tip that does not activate a window or steal game focus."""

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from config import ADVICE_DISPLAY_MS


class AdviceBubble(QFrame):
    geometry_changed = Signal()

    def __init__(self, parent):
        super().__init__(parent, (
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        ))
        self.setObjectName("adviceBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        heading = QLabel("CAPRI // HEADS UP")
        heading.setObjectName("adviceHeading")
        layout.addWidget(heading)
        self.text = QLabel()
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setWordWrap(True)
        layout.addWidget(self.text)
        self.setToolTip("Click to dismiss")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_tip(self, text):
        self.text.setText(text)
        self.adjustSize()
        owner = self.parentWidget()
        bounds = owner.screen().availableGeometry()
        x = max(bounds.left(), min(owner.x(), bounds.right() - self.width() + 1))
        y = owner.y() - self.height() - 8
        if y < bounds.top():
            y = min(bounds.bottom() - self.height() + 1, owner.y() + owner.height() + 8)
        self.move(x, y)
        self.show()
        self._timer.start(ADVICE_DISPLAY_MS)
        self.geometry_changed.emit()

    def mousePressEvent(self, event):
        self.hide()
        event.accept()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)
        self.geometry_changed.emit()

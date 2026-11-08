"""A modeless text conversation beside the desktop companion."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)


class ChatPanel(QDialog):
    message_submitted = Signal(str, bool)
    stop_requested = Signal()
    geometry_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Talk to Capri")
        self.setObjectName("chatPanel")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(440, 480)
        self.setMinimumSize(340, 320)
        layout = QVBoxLayout(self)
        title = QLabel("CAPRI // COMMS")
        title.setObjectName("chatTitle")
        layout.addWidget(title)
        self.transcript = QPlainTextEdit()
        self._partial_block = None
        self.transcript.setReadOnly(True)
        self.transcript.setMaximumBlockCount(1000)
        self.transcript.setAccessibleName("Conversation history")
        self.transcript.setPlaceholderText("Type a message, or turn on the microphone to talk.")
        layout.addWidget(self.transcript)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setMaxLength(4000)
        self.input.setPlaceholderText("Message Capri...")
        self.input.setAccessibleName("Message Capri")
        self.input.returnPressed.connect(self.submit)
        row.addWidget(self.input, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setDefault(False)
        self.send_button.setAutoDefault(False)
        self.send_button.clicked.connect(self.submit)
        row.addWidget(self.send_button)
        layout.addLayout(row)
        options = QHBoxLayout()
        self.speak_replies = QCheckBox("Speak typed replies")
        options.addWidget(self.speak_replies)
        options.addStretch()
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.setAutoDefault(False)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        options.addWidget(self.stop_button)
        layout.addLayout(options)

    def submit(self):
        text = self.input.text().strip()
        if text and self.send_button.isEnabled():
            self.input.clear()
            self.message_submitted.emit(text, self.speak_replies.isChecked())

    def append_message(self, speaker, text):
        # Plain text prevents model output from executing HTML or loading URLs.
        if speaker == "Capri" and self._partial_block is not None:
            self.show_partial(text)
            self._partial_block = None
            self.transcript.appendPlainText("")
            return
        self._partial_block = None
        self.transcript.appendPlainText(f"{speaker}\n{text}\n")
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def show_partial(self, text):
        if self._partial_block is None:
            self.transcript.appendPlainText("Capri\n")
            self._partial_block = self.transcript.document().lastBlock()
        cursor = QTextCursor(self.transcript.document())
        cursor.setPosition(self._partial_block.position())
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text)
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_busy(self, busy, microphone_active=False, stopping=False, can_replace=False):
        self.send_button.setEnabled(not busy or (can_replace and not stopping and not microphone_active))
        self.speak_replies.setEnabled(not busy)
        self.stop_button.setEnabled(busy and not stopping)
        self.input.setPlaceholderText(
            "Stop the microphone to send a typed message."
            if microphone_active else "Message Capri..."
        )

    def moveEvent(self, event):
        super().moveEvent(event)
        self.geometry_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.geometry_changed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        self.geometry_changed.emit()
        self.input.setFocus()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.geometry_changed.emit()

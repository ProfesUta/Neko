from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.workers import ChatWorker, VoiceWorker


class NekoWindow(QMainWindow):
    def __init__(self, messages, context):
        super().__init__()

        self.messages = messages
        self.context = context

        self.worker = None
        self.voice_worker = None

        self.setWindowTitle("Neko")
        self.resize(900, 650)

        self.setup_ui()

    # ======================================================
    # UI SETUP
    # ======================================================

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(
            central_widget
        )

        main_layout.setContentsMargins(
            18,
            12,
            18,
            12
        )

        main_layout.setSpacing(0)

        # ==================================================
        # HEADER
        # ==================================================

        header_layout = QHBoxLayout()

        header_layout.setContentsMargins(
            8,
            4,
            8,
            14
        )

        avatar = QLabel("🐱")

        avatar.setObjectName(
            "nekoAvatar"
        )

        avatar.setFixedSize(
            48,
            48
        )

        avatar.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        identity_layout = QVBoxLayout()

        identity_layout.setSpacing(
            1
        )

        title = QLabel("Neko")

        title.setObjectName(
            "titleLabel"
        )

        title.setFont(
            QFont(
                "Segoe UI",
                20,
                QFont.Weight.DemiBold
            )
        )

        status_layout = QHBoxLayout()

        status_layout.setSpacing(
            5
        )

        online_dot = QLabel("●")

        online_dot.setObjectName(
            "onlineDot"
        )

        self.status_label = QLabel(
            "Online"
        )

        self.status_label.setObjectName(
            "statusLabel"
        )

        status_layout.addWidget(
            online_dot
        )

        status_layout.addWidget(
            self.status_label
        )

        status_layout.addStretch()

        identity_layout.addWidget(
            title
        )

        identity_layout.addLayout(
            status_layout
        )

        header_layout.addWidget(
            avatar
        )

        header_layout.addSpacing(
            10
        )

        header_layout.addLayout(
            identity_layout
        )

        header_layout.addStretch()

        main_layout.addLayout(
            header_layout
        )

        # ==================================================
        # CHAT AREA
        # ==================================================

        self.chat_scroll = QScrollArea()

        self.chat_scroll.setObjectName(
            "chatScroll"
        )

        self.chat_scroll.setWidgetResizable(
            True
        )

        self.chat_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.chat_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        self.chat_scroll.setFrameShape(
            QFrame.Shape.NoFrame
        )

        self.chat_container = QWidget()

        self.chat_container.setObjectName(
            "chatContainer"
        )

        self.chat_layout = QVBoxLayout(
            self.chat_container
        )

        self.chat_layout.setContentsMargins(
            8,
            10,
            8,
            10
        )

        self.chat_layout.setSpacing(
            22
        )

        self.chat_layout.addStretch()

        self.chat_scroll.setWidget(
            self.chat_container
        )

        main_layout.addWidget(
            self.chat_scroll,
            1
        )

        # Initial message.

        self.add_message(
            "Neko",
            "Hmph. You're finally here, Dad."
        )

        # ==================================================
        # COMPOSER
        # ==================================================

        composer_frame = QFrame()

        composer_frame.setObjectName(
            "composerFrame"
        )

        composer_layout = QHBoxLayout(
            composer_frame
        )

        composer_layout.setContentsMargins(
            8,
            7,
            8,
            7
        )

        composer_layout.setSpacing(
            7
        )

        self.input_box = QLineEdit()

        self.input_box.setObjectName(
            "messageInput"
        )

        self.input_box.setPlaceholderText(
            "Message Neko..."
        )

        self.voice_button = QPushButton(
            "🎤"
        )

        self.voice_button.setObjectName(
            "voiceButton"
        )

        self.voice_button.setFixedSize(
            42,
            42
        )

        self.send_button = QPushButton(
            "➤"
        )

        self.send_button.setObjectName(
            "sendButton"
        )

        self.send_button.setFixedSize(
            42,
            42
        )

        composer_layout.addWidget(
            self.input_box,
            1
        )

        composer_layout.addWidget(
            self.voice_button
        )

        composer_layout.addWidget(
            self.send_button
        )

        main_layout.addSpacing(
            10
        )

        main_layout.addWidget(
            composer_frame
        )

        # ==================================================
        # CONNECTIONS
        # ==================================================

        self.send_button.clicked.connect(
            self.send_message
        )

        self.input_box.returnPressed.connect(
            self.send_message
        )

        self.voice_button.clicked.connect(
            self.toggle_voice
        )

    # ======================================================
    # CHAT MESSAGE
    # ======================================================

    def add_message(self, sender, text):
        message_container = QWidget()

        message_layout = QVBoxLayout(
            message_container
        )

        message_layout.setContentsMargins(
            0,
            0,
            0,
            0
        )

        message_layout.setSpacing(
            6
        )

        # ==================================================
        # CENTERED TIME
        # ==================================================

        time_label = QLabel(
            self.current_time()
        )

        time_label.setObjectName(
            "messageTime"
        )

        time_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        message_layout.addWidget(
            time_label
        )

        # ==================================================
        # MESSAGE ROW
        # ==================================================

        row = QWidget()

        row_layout = QHBoxLayout(
            row
        )

        row_layout.setContentsMargins(
            0,
            0,
            0,
            0
        )

        row_layout.setSpacing(
            8
        )

        # ==================================================
        # NEKO MESSAGE
        # ==================================================

        if sender == "Neko":

            avatar = QLabel("🐱")

            avatar.setObjectName(
                "nekoAvatar"
            )

            avatar.setFixedSize(
                42,
                42
            )

            avatar.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )

            bubble = QLabel(
                text
            )

            bubble.setObjectName(
                "nekoBubble"
            )

            bubble.setWordWrap(
                True
            )

            bubble.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )

            # Long Neko bubbles.

            bubble.setMaximumWidth(
                700
            )

            row_layout.addWidget(
                avatar,
                0,
                Qt.AlignmentFlag.AlignBottom
            )

            row_layout.addWidget(
                bubble,
                0,
                Qt.AlignmentFlag.AlignBottom
            )

            row_layout.addStretch()

        # ==================================================
        # USER MESSAGE
        # ==================================================

        else:

            bubble = QLabel(
                text
            )

            bubble.setObjectName(
                "userBubble"
            )

            bubble.setWordWrap(
                True
            )

            bubble.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )

            # User bubbles are also substantial,
            # but smaller than Neko's maximum width.


            bubble.setMaximumWidth(
                520
            )

            row_layout.addStretch()

            row_layout.addWidget(
                bubble,
                0,
                Qt.AlignmentFlag.AlignBottom
            )

        # Add message before bottom spacer.

        self.chat_layout.insertWidget(
            self.chat_layout.count() - 1,
            message_container
        )

        message_layout.addWidget(
            row
        )

        self.scroll_to_bottom()

    # ======================================================
    # TIME
    # ======================================================

    def current_time(self):
        return datetime.now().strftime(
            "%H:%M"
        )

    # ======================================================
    # SCROLL
    # ======================================================

    def scroll_to_bottom(self):
        scrollbar = (
            self.chat_scroll.verticalScrollBar()
        )

        scrollbar.setValue(
            scrollbar.maximum()
        )

    # ======================================================
    # TEXT CHAT
    # ======================================================

    def send_message(self):
        if self.voice_worker is not None:
            return

        text = self.input_box.text().strip()

        if not text:
            return

        self.add_message(
            "You",
            text
        )

        self.input_box.clear()

        self.input_box.setEnabled(
            False
        )

        self.send_button.setEnabled(
            False
        )

        self.status_label.setText(
            "Thinking..."
        )

        self.messages.append({
            "role": "user",
            "content": text
        })

        self.worker = ChatWorker(
            self.messages,
            self.context
        )

        self.worker.response_ready.connect(
            self.handle_response
        )

        self.worker.error.connect(
            self.handle_error
        )

        self.worker.finished.connect(
            self.worker_finished
        )

        self.worker.start()

    # ======================================================
    # VOICE MODE
    # ======================================================

    def toggle_voice(self):
        if self.voice_worker is None:
            self.start_voice()
        else:
            self.stop_voice()

    def start_voice(self):
        self.input_box.setEnabled(
            False
        )

        self.send_button.setEnabled(
            False
        )

        self.voice_button.setEnabled(
            True
        )

        self.voice_button.setText(
            "⏹"
        )

        self.status_label.setText(
            "Listening..."
        )

        self.voice_worker = VoiceWorker(
            self.messages,
            self.context
        )

        self.voice_worker.status_changed.connect(
            self.handle_voice_status
        )

        self.voice_worker.transcription_ready.connect(
            self.handle_transcription
        )

        self.voice_worker.response_ready.connect(
            self.handle_response
        )

        self.voice_worker.error.connect(
            self.handle_error
        )

        self.voice_worker.finished.connect(
            self.voice_worker_finished
        )

        self.voice_worker.start()

    def stop_voice(self):
        if self.voice_worker is None:
            return

        self.status_label.setText(
            "Stopping..."
        )

        self.voice_button.setEnabled(
            False
        )

        self.voice_worker.stop()

    # ======================================================
    # VOICE STATUS
    # ======================================================

    def handle_voice_status(self, status):
        self.status_label.setText(
            status
        )

    def handle_transcription(self, text):
        self.add_message(
            "You",
            text
        )

    # ======================================================
    # RESPONSE
    # ======================================================

    def handle_response(self, answer):
        self.add_message(
            "Neko",
            answer
        )

    def handle_error(self, error):
        self.add_message(
            "Neko",
            "Something went wrong."
        )

        self.add_message(
            "Neko",
            f"[Error] {error}"
        )

        self.status_label.setText(
            "Error"
        )

    # ======================================================
    # WORKER CLEANUP
    # ======================================================

    def worker_finished(self):
        self.worker = None

        self.input_box.setEnabled(
            True
        )

        self.send_button.setEnabled(
            True
        )

        self.voice_button.setEnabled(
            True
        )

        self.status_label.setText(
            "Online"
        )

        self.input_box.setFocus()

    def voice_worker_finished(self):
        self.voice_worker = None

        self.input_box.setEnabled(
            True
        )

        self.send_button.setEnabled(
            True
        )

        self.voice_button.setEnabled(
            True
        )

        self.voice_button.setText(
            "🎤"
        )

        self.status_label.setText(
            "Online"
        )

        self.input_box.setFocus()

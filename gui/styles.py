DARK_THEME = """
QWidget#overlayRoot {
    background: transparent;
    color: #e6fbff;
    font-family: "Segoe UI";
}

QLabel#statusLabel {
    background: rgba(5, 15, 23, 185);
    border: 1px solid rgba(83, 232, 244, 110);
    border-radius: 10px;
    color: #b7f7ff;
    font-size: 10px;
    font-weight: 600;
    padding: 4px 9px;
}

QFrame#controlsPopup {
    background: #081824;
    border: 1px solid #315969;
    border-radius: 12px;
    color: #c8fbff;
    font-family: "Segoe UI";
}

QPushButton#voiceButton, QPushButton#visionButton, QPushButton#closeButton {
    background: rgba(5, 15, 23, 210);
    border: 1px solid rgba(83, 232, 244, 150);
    border-radius: 15px;
    color: #c8fbff;
    font-size: 11px;
    font-weight: 600;
    padding: 6px 12px;
}

QPushButton#chatButton, QPushButton#stopButton, QPushButton#adviceButton {
    background: rgba(5, 15, 23, 210);
    border: 1px solid rgba(83, 232, 244, 110);
    border-radius: 10px;
    color: #c8fbff;
    font-size: 10px;
    font-weight: 600;
    padding: 6px 12px;
}

QPushButton#chatButton:hover, QPushButton#stopButton:hover, QPushButton#adviceButton:hover {
    background: rgba(18, 53, 65, 230);
    border-color: #53e8f4;
}

QPushButton#stopButton:enabled {
    border-color: #ff6688;
    color: #ffe8ed;
}

QPushButton#stopButton:disabled {
    color: #8b969c;
    border-color: rgba(139, 150, 156, 70);
}

QPushButton#adviceButton:checked {
    border-color: #53e8f4;
    background: #12404b;
}

QFrame#adviceBubble {
    background: #0b1a27;
    border: 1px solid #53e8f4;
    border-radius: 10px;
}

QFrame#adviceBubble QLabel {
    color: #e6fbff;
    font-family: "Segoe UI";
    font-size: 12px;
    border: none;
}

QFrame#adviceBubble QLabel#adviceHeading {
    color: #53e8f4;
    font-size: 10px;
    font-weight: 700;
}

QDialog#chatPanel {
    background: #0b141e;
    color: #d6f5fc;
    font-family: "Segoe UI";
    font-size: 12px;
}

QDialog#chatPanel QLabel, QDialog#chatPanel QCheckBox {
    color: #b7d5df;
}

QDialog#chatPanel QLabel#chatTitle {
    color: #53e8f4;
    font-size: 15px;
    font-weight: 700;
    padding: 6px 0;
}

QDialog#chatPanel QPlainTextEdit, QDialog#chatPanel QLineEdit {
    background: #101f2c;
    color: #e6fbff;
    border: 1px solid #254858;
    border-radius: 8px;
    padding: 9px;
    selection-background-color: #225668;
}

QDialog#chatPanel QLineEdit:focus {
    border-color: #53e8f4;
}

QDialog#chatPanel QPushButton {
    background: #153040;
    color: #c8fbff;
    border: 1px solid #315969;
    border-radius: 8px;
    padding: 8px 16px;
}

QDialog#chatPanel QPushButton:hover {
    border-color: #53e8f4;
}

QDialog#chatPanel QPushButton:disabled {
    color: #758a94;
    border-color: #253b48;
}

QPushButton#voiceButton:hover, QPushButton#visionButton:hover, QPushButton#closeButton:hover {
    background: rgba(18, 53, 65, 230);
    border-color: #53e8f4;
}

QPushButton#closeButton {
    border-color: #ff6688;
    border-radius: 10px;
    color: #ffe8ed;
    padding: 8px 12px;
}

QPushButton#voiceButton:checked {
    background: rgba(111, 34, 50, 225);
    border-color: #ff6688;
    color: #ffe8ed;
}

QPushButton#visionButton:checked {
    background: rgba(12, 72, 81, 225);
    border-color: #53e8f4;
    color: #e6fbff;
}

QPushButton#voiceButton:disabled, QPushButton#visionButton:disabled {
    color: #8b969c;
    border-color: rgba(139, 150, 156, 100);
}
"""

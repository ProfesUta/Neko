DARK_THEME = """
/* =========================================================
   MAIN WINDOW
   ========================================================= */

QMainWindow {
    background-color: #0d0b12;
}

QWidget {
    background-color: #0d0b12;
    color: #f3edf8;
    font-family: "Segoe UI";
    font-size: 14px;
}


/* =========================================================
   HEADER
   ========================================================= */

QLabel#titleLabel {
    color: #f7f0fb;
    font-size: 22px;
    font-weight: 600;
}

QLabel#statusLabel {
    color: #a79caf;
    font-size: 12px;
}

QLabel#onlineDot {
    color: #9b5de5;
    font-size: 11px;
}


/* =========================================================
   CHAT AREA
   ========================================================= */

QScrollArea#chatScroll {
    background-color: #0d0b12;
    border: none;
}

QWidget#chatContainer {
    background-color: #0d0b12;
}


/* =========================================================
   MESSAGE TIME
   ========================================================= */

QLabel#messageTime {
    background-color: transparent;
    color: #817889;

    font-size: 11px;

    padding: 0px;
}


/* =========================================================
   NEKO AVATAR
   ========================================================= */

QLabel#nekoAvatar {
    background-color: #251d30;

    border: 1px solid #3c2c4d;
    border-radius: 22px;

    color: #c79bea;

    font-size: 19px;

    padding: 0px;
}


/* =========================================================
   NEKO MESSAGE
   ========================================================= */

QLabel#nekoBubble {
    background-color: #272131;

    color: #eee7f4;

    border: 1px solid #342b40;
    border-radius: 19px;

    padding: 11px 18px;

    font-size: 14px;
}


/* =========================================================
   USER MESSAGE
   ========================================================= */

QLabel#userBubble {
    background-color: #70469b;

    color: #ffffff;

    border: 1px solid #8356b2;
    border-radius: 19px;

    padding: 11px 18px;

    font-size: 14px;
}


/* =========================================================
   COMPOSER
   ========================================================= */

QFrame#composerFrame {
    background-color: #15111b;

    border: 1px solid #30243d;
    border-radius: 18px;
}


/* =========================================================
   MESSAGE INPUT
   ========================================================= */

QLineEdit#messageInput {
    background-color: transparent;

    border: none;

    color: #f3edf8;

    padding: 11px 14px;

    font-size: 14px;
}

QLineEdit#messageInput:focus {
    border: none;
}

QLineEdit#messageInput::placeholder {
    color: #746b7d;
}


/* =========================================================
   VOICE BUTTON
   ========================================================= */

QPushButton#voiceButton {
    background-color: #282033;

    border: 1px solid #3e3050;
    border-radius: 17px;

    color: #d7b5f0;

    font-size: 17px;
}

QPushButton#voiceButton:hover {
    background-color: #352743;

    border: 1px solid #75549a;
}

QPushButton#voiceButton:pressed {
    background-color: #443054;
}

QPushButton#voiceButton:disabled {
    background-color: #1d1822;

    border: 1px solid #29222f;

    color: #62596a;
}


/* =========================================================
   SEND BUTTON
   ========================================================= */

QPushButton#sendButton {
    background-color: #7549a2;

    border: 1px solid #885bb6;
    border-radius: 17px;

    color: #ffffff;

    font-size: 18px;
}

QPushButton#sendButton:hover {
    background-color: #8254b0;

    border: 1px solid #9b6ac8;
}

QPushButton#sendButton:pressed {
    background-color: #643f8b;
}

QPushButton#sendButton:disabled {
    background-color: #302638;

    border: 1px solid #40334a;

    color: #6d6377;
}


/* =========================================================
   SCROLLBAR
   ========================================================= */

QScrollBar:vertical {
    background: transparent;

    width: 8px;

    margin: 4px 2px 4px 0px;
}

QScrollBar::handle:vertical {
    background: #3b3048;

    border-radius: 4px;

    min-height: 40px;
}

QScrollBar::handle:vertical:hover {
    background: #554364;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
"""
from PySide6.QtCore import QThread, Signal


class ChatWorker(QThread):
    response_ready = Signal(str)
    error = Signal(str)

    def __init__(self, messages, context):
        super().__init__()

        self.messages = messages
        self.context = context

    def run(self):
        try:
            from core.chat import chat
            from voice.tts import speak

            answer = chat(
                self.messages,
                self.context,
                speak_response=False
            )

            # Show text first.

            self.response_ready.emit(
                answer
            )

            # Then speak.

            speak(answer)

        except Exception as e:
            self.error.emit(
                str(e)
            )


class VoiceWorker(QThread):
    response_ready = Signal(str)
    transcription_ready = Signal(str)
    status_changed = Signal(str)
    error = Signal(str)

    def __init__(self, messages, context):
        super().__init__()

        self.messages = messages
        self.context = context

        self.running = True

    def stop(self):
        self.running = False
        self.requestInterruption()

    def run(self):
        try:
            from voice.voice import (
                record_until_silence,
                transcribe_audio,
            )

            from core.chat import chat
            from voice.tts import speak

            # ==================================================
            # CONTINUOUS VOICE LOOP
            # ==================================================

            while self.running:

                # --------------------------------------------------
                # LISTEN
                # --------------------------------------------------

                self.status_changed.emit(
                    "🎤 Listening..."
                )

                audio = record_until_silence()

                if not self.running:
                    break

                if audio is None:
                    continue

                # --------------------------------------------------
                # TRANSCRIBE
                # --------------------------------------------------

                self.status_changed.emit(
                    "📝 Transcribing..."
                )

                text = transcribe_audio(
                    audio
                )

                if not self.running:
                    break

                if not text:
                    self.status_changed.emit(
                        "I didn't understand that"
                    )
                    continue

                # Show what the user said.

                self.transcription_ready.emit(
                    text
                )

                self.messages.append({
                    "role": "user",
                    "content": text
                })

                # --------------------------------------------------
                # THINK
                # --------------------------------------------------

                self.status_changed.emit(
                    "🧠 Thinking..."
                )

                answer = chat(
                    self.messages,
                    self.context,
                    speak_response=False
                )

                if not self.running:
                    break

                # --------------------------------------------------
                # SHOW TEXT FIRST
                # --------------------------------------------------

                self.response_ready.emit(
                    answer
                )

                # --------------------------------------------------
                # SPEAK SECOND
                # --------------------------------------------------

                self.status_changed.emit(
                    "🔊 Speaking..."
                )

                speak(answer)

                if not self.running:
                    break

                # --------------------------------------------------
                # AUTOMATICALLY LISTEN AGAIN
                # --------------------------------------------------

                self.status_changed.emit(
                    "🎤 Listening..."
                )

        except Exception as e:
            self.error.emit(
                str(e)
            )

            self.status_changed.emit(
                "Error"
            )

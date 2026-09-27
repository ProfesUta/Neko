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

            answer = chat(
                self.messages,
                self.context
            )

            self.response_ready.emit(answer)

        except Exception as e:
            self.error.emit(str(e))


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

    def run(self):
        try:
            from voice.voice import (
                record_until_silence,
                transcribe_audio,
            )

            from core.chat import chat

            while self.running:

                # ------------------------------
                # LISTEN
                # ------------------------------

                self.status_changed.emit(
                    "🎤 Listening..."
                )

                audio = record_until_silence()

                if not self.running:
                    break

                if audio is None:
                    continue

                # ------------------------------
                # TRANSCRIBE
                # ------------------------------

                self.status_changed.emit(
                    "📝 Transcribing..."
                )

                text = transcribe_audio(audio)

                if not self.running:
                    break

                if not text:
                    continue

                self.transcription_ready.emit(text)

                self.messages.append({
                    "role": "user",
                    "content": text
                })

                # ------------------------------
                # THINK
                # ------------------------------

                self.status_changed.emit(
                    "🧠 Thinking..."
                )

                answer = chat(
                    self.messages,
                    self.context
                )

                if not self.running:
                    break

                self.response_ready.emit(answer)

                # ------------------------------
                # LISTEN AGAIN
                # ------------------------------

                self.status_changed.emit(
                    "🎤 Listening..."
                )

        except Exception as e:
            self.error.emit(str(e))

        finally:
            self.running = False

import time
from threading import Lock

from config import VISION_CONTEXT_MAX_AGE_SECONDS
from core.interaction import InteractionState


class Context:
    def __init__(self):
        self.interaction = InteractionState()
        self.spatial = None
        self.last_application = None
        self.last_folder = None
        self.last_file = None
        self.last_url = None
        self.last_search = None
        self._screen_observation = ""
        self._screen_observation_at = 0.0
        self._screen_observation_lock = Lock()
        self._screen_reader = None
        self._screen_error = ""

    def set_application(self, application):
        self.last_application = application

    def set_folder(self, folder):
        self.last_folder = folder

    def set_file(self, file):
        self.last_file = file

    def set_url(self, url):
        self.last_url = url

    def set_search(self, search):
        self.last_search = search

    def set_screen_observation(self, observation, captured_at=None):
        with self._screen_observation_lock:
            captured_at = time.monotonic() if captured_at is None else captured_at
            if captured_at < self._screen_observation_at:
                return
            self._screen_observation = observation.strip()
            self._screen_observation_at = captured_at
            self._screen_error = ""

    def get_screen_observation(self):
        with self._screen_observation_lock:
            if (
                time.monotonic() - self._screen_observation_at
                > VISION_CONTEXT_MAX_AGE_SECONDS
            ):
                return ""
            return self._screen_observation

    def clear_screen_observation(self):
        with self._screen_observation_lock:
            self._screen_observation = ""
            self._screen_observation_at = 0.0

    def set_screen_reader(self, reader):
        with self._screen_observation_lock:
            self._screen_reader = reader
            self._screen_observation = ""
            self._screen_observation_at = 0.0
            self._screen_error = ""

    def refresh_screen(self, question, cancel_event=None, conversation=None):
        """Called on the conversation worker, never on the GUI thread."""
        with self._screen_observation_lock:
            reader = self._screen_reader
        if reader is None:
            return
        try:
            if hasattr(reader, "set_conversation"):
                reader.set_conversation(conversation or [])
            reading = reader.describe_question(question, cancel_event)
            text, captured_at = reading
        except Exception as error:
            from core.cancellation import check_cancelled
            check_cancelled(cancel_event)
            with self._screen_observation_lock:
                if reader is self._screen_reader:
                    self._screen_observation = ""
                    self._screen_observation_at = 0.0
                    self._screen_error = str(error) or "Screen reading was stopped."
            return
        with self._screen_observation_lock:
            if reader is self._screen_reader and not reader.isInterruptionRequested():
                self._screen_observation = text
                self._screen_observation_at = captured_at
                self._screen_error = ""
                if time.monotonic() - captured_at <= VISION_CONTEXT_MAX_AGE_SECONDS:
                    return getattr(reading, "direct_answer", None)

    def get_screen_status(self):
        with self._screen_observation_lock:
            age = time.monotonic() - self._screen_observation_at
            return {
                "enabled": self._screen_reader is not None,
                "available": bool(self._screen_observation) and age <= VISION_CONTEXT_MAX_AGE_SECONDS,
                "age_seconds": round(age, 1) if self._screen_observation else None,
                "error": self._screen_error,
            }

    def is_screen_source_current(self, source):
        with self._screen_observation_lock:
            reader = self._screen_reader
        return (
            reader is not None and not reader.isInterruptionRequested()
            and reader.is_source_current(source)
        )

    def get_summary(self):
        return {
            "last_application": self.last_application,
            "last_folder": self.last_folder,
            "last_file": self.last_file,
            "last_url": self.last_url,
            "last_search": self.last_search,
        }

    def get_text(self):
        summary = self.get_summary()

        lines = []

        if summary["last_application"]:
            lines.append(
                f"Last application: {summary['last_application']}"
            )

        if summary["last_folder"]:
            lines.append(
                f"Last folder: {summary['last_folder']}"
            )

        if summary["last_file"]:
            lines.append(
                f"Last file: {summary['last_file']}"
            )

        if summary["last_url"]:
            lines.append(
                f"Last URL: {summary['last_url']}"
            )

        if summary["last_search"]:
            lines.append(
                f"Last web search: {summary['last_search']}"
            )

        if not lines:
            return "No recent context."

        return "\n".join(lines)

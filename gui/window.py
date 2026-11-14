import random
import time
import logging
import weakref
import math

from config import (
    ADVICE_ENABLED_BY_DEFAULT, PET_MOVE_INTERVAL_MS, VISION_CHANGE_THRESHOLD, VISION_ENABLED_BY_DEFAULT,
    VISION_INTERVAL_SECONDS, VISION_MAX_IMAGE_SIZE, VISION_MODEL,
)
from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget,
)

from core.proactivity import AdvicePolicy
from core.league_voice import LeagueAnnouncement, LeagueVoicePolicy
from core.companion_style import reply_delivery
from core.monitors import MonitorManager
from core.spatial import SpatialService, spatial_request
from core.window_grounding import WindowGrounder
import config
from gui.advice_bubble import AdviceBubble
from gui.chat_panel import ChatPanel
from gui.controls import ControlsPopup
from gui.sprites import CharacterSprite
from gui.teleport import TeleportEffects, clamp_to_displays
from gui.spatial import MovementCoordinator
from gui.vision import VisionWorker
from gui.workers import AdviceWorker, ChatWorker, VoiceWarmupWorker, VoiceWorker, LeagueWorker


class CapriWindow(QMainWindow):
    def __init__(self, messages, context):
        super().__init__()
        self.messages = messages
        self.context = context
        from gui.league_replay import ReplayService
        self.replay_service = ReplayService(context.league,self)
        context.league_replay = self.replay_service
        self.replay_service.stopped.connect(self._finish_close_if_ready)
        from gui.league_vision import LeagueVisualService
        self.league_visual_service=LeagueVisualService(context.league,self)
        context.league_visual=self.league_visual_service
        self.league_visual_service.stopped.connect(self._finish_close_if_ready)
        self.league_visual_service.notice.connect(self._replay_notice)
        self.voice_worker = None
        self.voice_warmup_worker = None
        self._voice_preload_status = "Voice models load when enabled"
        self.chat_worker = None
        self.vision_worker = None
        self.advice_worker = None
        self.league_worker = None
        self._league_voice_policy = LeagueVoicePolicy(on_status=context.league.announcement_status)
        self._drag_offset = None
        self._press_position = None
        self._dragging = False
        self._closing = False
        self._stopping = False
        self._inference_busy = False
        self._status = "Standby"
        self._last_error = None
        self._vision_error = None
        self._reported_vision_errors = {}
        self._movement_target = None
        self._movement_state = "idle"
        self._next_wander_at = time.monotonic() + 2
        self._glitch_steps = 0
        self._next_processing_teleport_at = 0.0
        self.teleport_effects = None
        self._celebrate_reply = False
        self._reply_expression = None
        self._chat_positioned = False
        self._advice_enabled = ADVICE_ENABLED_BY_DEFAULT
        self._advice_policy = AdvicePolicy()
        self._pending_advice = None
        self._deferred_screen_advice = None
        self._pending_action = None
        self._league_mode_active = False
        self._tts_active = False
        self._league_window_bounds = None
        self._league_monitor_id = None

        self.setWindowTitle("Capri")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.setFixedSize(330, 350)
        self.setup_ui()
        self.chat_panel = ChatPanel(self)
        self.replay_service.notice.connect(self._replay_notice)
        self.chat_panel.message_submitted.connect(self.send_text)
        self.chat_panel.stop_requested.connect(self.stop_current_turn)
        self.chat_panel.geometry_changed.connect(self._update_vision_geometry)
        self.advice_bubble = AdviceBubble(self)
        self.advice_bubble.geometry_changed.connect(self._update_vision_geometry)
        window_ref = weakref.ref(self)
        def protected_sprite():
            window = window_ref()
            return ((window._sprite_screen_rect(), window.sprite.teleport_silhouette())
                    if window is not None else (QRectF(), None))
        def screen_reader():
            window = window_ref()
            return window.vision_worker if window is not None else None
        def screen_enabled():
            reader = screen_reader()
            return reader is not None and not reader.isInterruptionRequested()
        self.teleport_effects = TeleportEffects(self, protected_sprite=protected_sprite)
        self.teleport_effects.effects_changed.connect(self._update_vision_geometry)
        self.monitor_manager = MonitorManager(self)
        self.window_grounder = WindowGrounder()
        self.movement_coordinator = MovementCoordinator(self, self.monitor_manager, self.window_grounder)
        self.context.spatial = SpatialService(
            self.monitor_manager, self.window_grounder, screen_reader, screen_enabled,
            self.movement_coordinator.dispatch,
        )
        self.movement_coordinator.highlight.changed.connect(self._update_vision_geometry)
        self.movement_coordinator.state_changed.connect(self._sync_controls)
        self.monitor_manager.changed.connect(self._on_monitors_changed)
        self._sync_controls()
        self.move_to_corner()
        self._movement_target = self.pos()
        self._movement_timer = QTimer(self)
        self._movement_timer.timeout.connect(self._move_pet)
        self._movement_timer.start(PET_MOVE_INTERVAL_MS)
        self._live_status_timer = QTimer(self)
        self._league_notice_timer = QTimer(self)
        self._league_notice_timer.setSingleShot(True)
        self._league_notice_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._league_notice_timer.timeout.connect(self._deliver_league_notification)
        self._live_status_timer.timeout.connect(self._sync_vision)
        self._live_status_timer.timeout.connect(self._deliver_league_notification)
        self._live_status_timer.timeout.connect(self._deliver_screen_advice)
        self._live_status_timer.start(1000)
        self._shortcuts = []
        for sequence, action in (
            ("Ctrl+J", self.toggle_chat), ("Ctrl+M", self.toggle_voice),
            ("Ctrl+.", self.stop_current_turn),
            ("Ctrl+T", self.teleport_randomly),
            ("Ctrl+Shift+L", self._open_league_calibration),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(action)
            self._shortcuts.append(shortcut)
        if VISION_ENABLED_BY_DEFAULT:
            self.start_vision()
        else:
            self._render_status()
        if config.VOICE_PRELOAD_ON_STARTUP:
            self.start_voice_preload()
        if config.LEAGUE_LIVE_DATA_ENABLED or config.LEAGUE_CHAMP_SELECT_ENABLED or config.LEAGUE_EVENTS_ENABLED:
            self.league_worker = LeagueWorker(self.context.league, self.window_grounder)
            self.league_worker.notification.connect(self.handle_league_notification)
            self.league_worker.session_active_changed.connect(self.set_league_companion_mode, Qt.ConnectionType.QueuedConnection)
            self.league_worker.session_active_changed.connect(self.replay_service.cancel, Qt.ConnectionType.QueuedConnection)
            self.league_worker.session_active_changed.connect(self.league_visual_service.sync, Qt.ConnectionType.QueuedConnection)
            self.league_worker.visual_state_changed.connect(self.league_visual_service.sync, Qt.ConnectionType.QueuedConnection)
            self.league_worker.session_display_changed.connect(self._league_display_changed, Qt.ConnectionType.QueuedConnection)
            self.league_worker.warning.connect(lambda text: self.chat_panel.append_message("League", text))
            self.league_worker.finished.connect(self.league_worker_finished)
            self.league_worker.start()

    def _replay_notice(self, text):
        if not self._closing:
            self.chat_panel.append_message("League replay",text)

    def league_worker_finished(self):
        worker, self.league_worker = self.league_worker, None
        if worker is not None:
            worker.deleteLater()
        if not self._closing:
            self.set_league_companion_mode(False)
        self._finish_close_if_ready()

    @property
    def league_mode_active(self):
        return self._league_mode_active

    @property
    def movement_locked(self):
        return self._league_mode_active and config.LEAGUE_LOCK_MOVEMENT

    def _league_display_changed(self, physical_bounds):
        if self._closing:
            return
        self._league_window_bounds = physical_bounds
        self._league_monitor_id = None
        if self._league_mode_active:
            self._dock_league(effects=False)

    def _league_monitor(self):
        monitors = self.monitor_manager.snapshot()
        chosen = next((monitor for monitor in monitors if monitor.id == self._league_monitor_id), None)
        if chosen is None and self._league_window_bounds is not None:
            candidate = max(monitors, key=lambda monitor: monitor.physical.intersection(self._league_window_bounds).area,
                            default=None)
            if candidate and candidate.physical.intersection(self._league_window_bounds).area:
                chosen = candidate
        chosen = chosen or next((monitor for monitor in monitors if monitor.primary), None) or next(iter(monitors), None)
        self._league_monitor_id = chosen.id if chosen else None
        return chosen

    def _league_visible_in_window(self):
        return self.sprite.league_visible_rect().translated(QPointF(self.sprite.mapTo(self, QPoint(0, 0))))

    def _dock_league(self, *, effects=False):
        if self._closing or not self._league_mode_active or not config.LEAGUE_DOCK_RIGHT_ENABLED:
            return
        monitor = self._league_monitor()
        if monitor is None:
            return
        margin = max(0, config.LEAGUE_DOCK_MARGIN_PX)
        self.sprite.set_league_available_size(monitor.available.width - margin * 2, monitor.available.height - margin * 2)
        self.centralWidget().layout().activate()
        visible = self._league_visible_in_window()
        x = round(monitor.available.right - margin - visible.right())
        y = round(monitor.available.center[1] - visible.center().y())
        pose = (lambda: self.sprite.set_league_mode(True)) if not self.sprite.league_mode else None
        self.teleport_to(x, y, effects=effects, arrival_pose=pose, league_anchor=True)

    def set_league_companion_mode(self, active):
        if self._closing:
            return
        active = bool(active) and config.LEAGUE_COMPANION_MODE_ENABLED
        if active == self._league_mode_active:
            return
        self._league_mode_active = active
        self.context.spatial.cancel()
        self.movement_coordinator.reset()
        self._drag_offset = self._press_position = None
        self._dragging = False
        self._movement_target = self.pos()
        self._glitch_steps = 0
        self.sprite.set_league_speaking(self._tts_active)
        if active:
            if config.LEAGUE_DOCK_RIGHT_ENABLED and self._league_monitor() is not None:
                self._dock_league(effects=True)
            else:
                self.sprite.set_league_mode(True)
        else:
            self.sprite.set_league_mode(False)
            self._league_monitor_id = None
            self._movement_state = self.sprite.state
            self._next_wander_at = time.monotonic() + random.uniform(2, 4)
            self._next_processing_teleport_at = self._next_wander_at

    def handle_league_notification(self, announcement):
        if self._closing or not self._advice_enabled or not config.LEAGUE_PROACTIVE_VOICE_ENABLED:
            self.context.league.announcement_status(announcement,"dropped")
            return
        self._league_voice_policy.offer(announcement)
        self._deliver_league_notification()

    def _deliver_league_notification(self):
        if self._closing or self._stopping or not self._advice_enabled or not config.LEAGUE_PROACTIVE_VOICE_ENABLED:
            self._league_voice_policy.clear()
            self._league_notice_timer.stop()
            return
        pending = self._league_voice_policy.pending
        if pending is not None and not self.context.league.announcement_current(pending):
            self._league_voice_policy.discard_pending()
            self._league_notice_timer.stop()
            return
        available = (pending is not None and self.chat_worker is None and self.advice_worker is None
                     and not self._inference_busy and self._pending_action is None and self._pending_advice is None
                     and (self.voice_worker is None or self.voice_worker.microphone_enabled())
                     and self.context.interaction.allows_advice(pending.captured_at)
                     and self.context.league.announcement_current(pending))
        announcement = self._league_voice_policy.take(available,defer_start=True)
        if announcement is None:
            ready_at = self._league_voice_policy.next_ready_at()
            if available and ready_at is not None:
                self._league_notice_timer.start(max(1, math.ceil((ready_at - time.monotonic()) * 1000)))
            return
        self._league_notice_timer.stop()
        if self.voice_worker is not None:
            if self.voice_worker.offer_advice(announcement):
                self._pending_advice = announcement
            else:self._league_voice_policy.finish(announcement)
        else:
            self._pending_advice = announcement
            self.advice_worker = AdviceWorker(self.messages, self.context, announcement)
            self._connect_worker(self.advice_worker, proactive=True)
            self.advice_worker.start()

    def start_voice_preload(self):
        if self._closing or self.voice_warmup_worker is not None:
            return
        self.voice_warmup_worker = VoiceWarmupWorker()
        self.voice_warmup_worker.status_changed.connect(self.handle_voice_preload_status)
        self.voice_warmup_worker.warning.connect(self.handle_voice_preload_warning)
        self.voice_warmup_worker.finished.connect(self.voice_preload_finished)
        self.handle_voice_preload_status("Preparing voice models")
        self.voice_warmup_worker.start()

    def handle_voice_preload_status(self, status):
        self._voice_preload_status = status
        if not self._closing:
            self._sync_controls()

    def handle_voice_preload_warning(self, message):
        logging.warning("%s", message)
        self.handle_voice_preload_status(message)

    def voice_preload_finished(self):
        worker = self.voice_warmup_worker
        self.voice_warmup_worker = None
        if worker is not None:
            worker.deleteLater()
        self._finish_close_if_ready()

    def setup_ui(self):
        central = QWidget(self)
        central.setObjectName("overlayRoot")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        self.sprite = CharacterSprite(central)
        self.sprite.setObjectName("characterSprite")
        self.sprite.setFixedSize(300, 300)
        self.sprite.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.sprite, 1, Qt.AlignmentFlag.AlignCenter)
        self.status_label = QLabel("STANDBY")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        self.controls_popup = ControlsPopup(self)
        self.controls_popup.action_requested.connect(self.handle_control_action)
        self.controls_popup.owner_right_clicked.connect(self.show_exit)
        self.controls_popup.geometry_changed.connect(self._update_vision_geometry)
        self.voice_button = self.controls_popup.voice_button
        self.vision_button = self.controls_popup.vision_button
        self.chat_button = self.controls_popup.chat_button
        self.stop_button = self.controls_popup.stop_button
        self.close_button = self.controls_popup.exit_button
        self.advice_button = self.controls_popup.advice_button

    def handle_control_action(self, action):
        actions = {
            "voice": self.toggle_voice, "vision": self.toggle_vision,
            "chat": self.toggle_chat, "stop": self.stop_current_turn,
            "exit": self.close, "advice": self.toggle_advice,
        }
        actions[action]()

    def toggle_controls(self, position):
        if self._closing:
            return
        if self.controls_popup.isVisible() and self.controls_popup.mode == "controls":
            self.controls_popup.hide()
        else:
            self.controls_popup.show_at("controls", position)

    def show_exit(self, position):
        if not self._closing:
            self.controls_popup.show_at("exit", position)

    def move_to_corner(self):
        screen = QGuiApplication.primaryScreen()
        if screen:
            bounds = screen.availableGeometry()
            self.move(bounds.right() - self.width() - 23, bounds.bottom() - self.height() - 23)

    def toggle_chat(self):
        if self.chat_panel.isVisible():
            self.chat_panel.hide()
        else:
            self.show_chat()

    def show_chat(self):
        if not self._chat_positioned:
            screen = self._active_screen()
            if screen:
                bounds = screen.availableGeometry()
                x = self.x() - self.chat_panel.width() - 12
                if x < bounds.left():
                    x = self.x() + self.width() + 12
                x = max(bounds.left(), min(x, bounds.right() - self.chat_panel.width() + 1))
                y = max(bounds.top(), min(self.y(), bounds.bottom() - self.chat_panel.height() + 1))
                self.chat_panel.move(x, y)
            self._chat_positioned = True
        self.chat_panel.show()
        self.chat_panel.raise_()
        self.chat_panel.activateWindow()
        self._update_vision_geometry()

    def _conversation_worker(self):
        return self.voice_worker or self.chat_worker or self.advice_worker

    def _connect_worker(self, worker, proactive=False):
        self._celebrate_reply = False
        self._reply_expression = None
        self._last_error = None
        self._stopping = False
        self._inference_busy = not proactive
        worker.status_changed.connect(self.handle_voice_status)
        worker.playback_active_changed.connect(self.handle_playback_active, Qt.ConnectionType.QueuedConnection)
        worker.user_text.connect(self.handle_user_text)
        worker.response_ready.connect(self.handle_response)
        worker.response_partial.connect(self.handle_partial_response)
        worker.reaction_ready.connect(self.handle_reply_reaction)
        worker.tool_result.connect(self.handle_tool_result)
        worker.inference_active.connect(self.handle_inference_active)
        worker.error.connect(self.handle_error)
        worker.interrupted.connect(self.handle_interrupted)
        worker.advice_ready.connect(self.handle_advice_delivery)
        worker.advice_finished.connect(self.handle_advice_finished)
        worker.advice_started.connect(self.handle_advice_started)
        worker.advice_error.connect(self.handle_advice_error)
        worker.finished.connect(self.conversation_worker_finished)
        self._sync_controls()
        self._sync_vision()

    def send_text(self, text, speak_reply=None):
        if speak_reply is None:
            speak_reply = self.chat_panel.speak_replies.isChecked()
        text = text.strip()
        if text and self.chat_worker is not None and not self._closing and (
            spatial_request(text) or spatial_request(self.chat_worker.text) or self.movement_coordinator.busy
        ):
            self._pending_action = ("text", text, speak_reply)
            self.context.spatial.cancel()
            self.movement_coordinator.reset()
            self.chat_worker.stop()
            self._stopping = True
            self.set_status("Stopping")
            self._sync_controls()
            return
        if text and self.advice_worker is not None and not self._closing:
            self._pending_action = ("text", text, speak_reply)
            self.advice_worker.stop()
            self.advice_bubble.hide()
            self._sync_controls()
            return
        if not text or self._conversation_worker() is not None or self._closing:
            return
        self.chat_worker = ChatWorker(self.messages, self.context, text, speak_reply)
        self._connect_worker(self.chat_worker)
        self.set_status("Thinking")
        self.chat_worker.start()

    def toggle_voice(self):
        if self._stopping or self._closing or self.chat_worker is not None:
            return
        if self.voice_worker is None:
            self.start_voice()
        else:
            self.stop_voice()

    def start_voice(self):
        if self.advice_worker is not None and not self._closing:
            self._pending_action = ("voice",)
            # Enabling input must not cancel output. Open recording after the
            # current notice finishes so Capri doesn't transcribe her own voice.
            self._sync_controls()
            return
        if self._conversation_worker() is not None or self._closing:
            return
        self.voice_worker = VoiceWorker(self.messages, self.context)
        self._connect_worker(self.voice_worker)
        self.set_status("Loading voice models")
        self.voice_worker.start()

    def stop_voice(self):
        if self.voice_worker is not None:
            # MIC controls recording only. STOP remains the explicit way to
            # cancel inference or cut off Capri's current spoken answer.
            self.voice_worker.mute_microphone()
            self._sync_controls()

    def stop_current_turn(self):
        self.context.spatial.cancel()
        self.movement_coordinator.reset()
        self._pending_action = None
        self._advice_policy.snooze()
        self.advice_bubble.hide()
        if self.voice_worker is not None:
            self.voice_worker.cancel_advice()
        worker = self._conversation_worker()
        if worker is None or self._stopping:
            return
        self._stopping = True
        self.set_status("Stopping")
        self._sync_controls()
        worker.stop()

    def _sync_controls(self):
        busy = self._conversation_worker() is not None
        microphone_active = self.voice_worker is not None and self.voice_worker.microphone_enabled()
        self.voice_button.setChecked(microphone_active)
        self.voice_button.setText("MIC ON" if microphone_active else "MIC OFF")
        self.voice_button.setToolTip("Microphone enabled. Click to mute input; Capri can keep speaking." if microphone_active
                                     else f"Microphone off. {self._voice_preload_status}. Click to enable it.")
        self.voice_button.setEnabled(self.chat_worker is None and not self._stopping and not self._closing
                                     and (self.voice_worker is None or microphone_active))
        self.stop_button.setEnabled(busy and not self._stopping)
        user_busy = self.voice_worker is not None or self.chat_worker is not None or self._pending_action is not None
        can_replace = self.chat_worker is not None and (
            spatial_request(self.chat_worker.text) or self.movement_coordinator.busy
        )
        self.chat_panel.set_busy(user_busy, microphone_active, self._stopping, can_replace)
        effective_advice = self._advice_enabled
        self.advice_button.setChecked(effective_advice)
        self.advice_button.setText("ADVICE ON" if effective_advice else "ADVICE OFF")

    def handle_user_text(self, text):
        self.chat_panel.append_message("You", text)

    def handle_response(self, text):
        self.chat_panel.append_message("Capri", text)
        question = next((item.get("content", "") for item in reversed(self.messages) if item.get("role") == "user"), "")
        self._reply_expression = reply_delivery(question, text).expression
        self._celebrate_reply = self.chat_worker is not None and bool(self._reply_expression)

    def handle_reply_reaction(self, expression):
        if (not self._closing and not self._stopping and config.COMPANION_REACTIONS_ENABLED
                and self.sender() is self._conversation_worker()):
            self.sprite.react(expression, config.COMPANION_REACTION_DURATION_MS)

    def handle_partial_response(self, text):
        if not self._closing and not self._stopping and self.sender() is self._conversation_worker():
            self.chat_panel.show_partial(text)

    def handle_tool_result(self, name, result):
        preview = result[:1600]
        if len(result) > 1600:
            preview += "\n[More results available in this conversation.]"
        self.chat_panel.append_message(f"Tool: {name}", preview)

    def handle_interrupted(self):
        self._tts_active = False
        self.sprite.set_league_speaking(False)
        self.sprite.clear_reaction()
        self.chat_panel.append_message("Status", "Stopped. Completed actions are retained.")

    def handle_voice_status(self, status):
        if not self._stopping and not self._closing:
            self.set_status(status)

    def handle_playback_active(self, active):
        if self.sender() is not self._conversation_worker() or (active and (self._closing or self._stopping)):
            return
        self._tts_active = active
        self.sprite.set_league_speaking(active)

    def handle_inference_active(self, active):
        self._inference_busy = active
        self._sync_vision()

    def handle_error(self, error):
        self._tts_active = False
        self.sprite.set_league_speaking(False)
        self.sprite.clear_reaction()
        self._last_error = error
        self.chat_panel.append_message("Status", f"Something failed: {error}")
        if not self._stopping:
            self.set_status("Error - open chat")
            self.status_label.setToolTip(error)
            self.chat_panel.status.setText(error)
            self.show_chat()

    def conversation_worker_finished(self):
        worker = self.sender()
        celebrate = (
            self._celebrate_reply and worker is self.chat_worker
            and not worker.isInterruptionRequested() and not self._last_error
        )
        if worker is self.voice_worker:
            self.voice_worker = None
        elif worker is self.chat_worker:
            self.chat_worker = None
        elif worker is self.advice_worker:
            self.advice_worker = None
        else:
            return
        self._tts_active = False
        self.sprite.set_league_speaking(False)
        worker.deleteLater()
        self._pending_advice = None
        self._stopping = False
        self._inference_busy = False
        self._sync_controls()
        self._sync_vision()
        pending = self._pending_action
        self._pending_action = None
        if pending is not None and not self._closing:
            if pending[0] == "text":
                self.send_text(pending[1], pending[2])
            else:
                self.start_voice()
            return
        if self._last_error:
            self.set_status("Error - open chat")
            self.chat_panel.status.setText(self._last_error)
        else:
            self.set_status("Standby")
            if celebrate and config.COMPANION_REACTIONS_ENABLED:
                self.sprite.flash_expression(self._reply_expression, config.COMPANION_REACTION_DURATION_MS)
        if not self._closing:
            self._deliver_screen_advice()
            self._deliver_league_notification()
        self._finish_close_if_ready()

    def set_status(self, status):
        self._status = status
        self._render_status()
        self.status_label.setToolTip(status)
        self.chat_panel.status.setText(status)
        normalized = status.lower()
        if normalized.startswith("speech detected"):
            activity = "speechDetected"
        elif normalized.startswith("listen"):
            activity = "listening"
        elif normalized.startswith(("load", "look", "locat", "transcrib", "think", "process", "using", "checking")):
            activity = "processing"
        elif normalized.startswith("speak"):
            activity = "speaking"
        else:
            activity = "idle"
        self.sprite.set_activity(activity)
        if activity == "speaking":
            self.movement_coordinator.speaking()
        self._set_movement_state(self.sprite.state)

    def _render_status(self):
        if self.vision_worker is not None and not self.vision_worker.isInterruptionRequested():
            screen = "SCREEN ERROR" if self._vision_error else "SCREEN ON"
        else:
            screen = "SCREEN ERROR" if self._vision_error else "SCREEN OFF"
        self.status_label.setText(f"{self._status.upper()} / {screen}")

    def toggle_advice(self):
        self._advice_enabled = not self._advice_enabled
        if self._advice_enabled and self.vision_worker is None:
            self.start_vision()
        if self.vision_worker is not None:
            self.vision_worker.set_advice_enabled(self._advice_enabled)
        if not self._advice_enabled:
            self._cancel_advice()
        self._sync_controls()

    def _cancel_advice(self):
        self._league_voice_policy.clear()
        self._league_notice_timer.stop()
        self._deferred_screen_advice = None
        self.advice_bubble.hide()
        if self.voice_worker is not None:
            self.voice_worker.cancel_advice()
        if self.advice_worker is not None:
            self.advice_worker.stop()
        self._pending_advice = None

    def handle_advice_candidate(self, advice):
        if (
            self._closing or self._stopping or not self._advice_enabled
            or self.vision_worker is None or self.vision_worker.isInterruptionRequested()
            or self._inference_busy or self.chat_worker is not None
            or self._pending_action is not None
            or not self.context.interaction.allows_advice(advice.captured_at)
            or self.context.interaction.repeats_recent_response(advice.text)
            or not self.vision_worker.is_source_current(advice.source)
            or not self._advice_policy.should_offer(advice, ignore_delivery_gap=True)
        ):
            return
        if (self._pending_advice is not None or self.advice_worker is not None
                or (self.voice_worker is not None and not self.voice_worker.microphone_enabled())
                or not self._advice_policy.should_offer(advice)):
            # Keep only the latest useful notice. Muting input must not strand
            # a notice on the listener that is about to exit. Revalidate before
            # sending it to the existing speech worker after that worker ends.
            self._deferred_screen_advice = advice
            return
        if self.voice_worker is not None:
            if self.voice_worker.offer_advice(advice):
                self._advice_policy.reserve(advice)
                self._pending_advice = advice
        elif self.advice_worker is None:
            self._advice_policy.reserve(advice)
            self._pending_advice = advice
            self.advice_worker = AdviceWorker(self.messages, self.context, advice)
            self._connect_worker(self.advice_worker, proactive=True)
            self.advice_worker.start()

    def _deliver_screen_advice(self):
        advice = self._deferred_screen_advice
        if advice is None:
            return
        if self._closing or not self._advice_enabled or not advice.is_fresh():
            self._deferred_screen_advice = None
            return
        if (self.chat_worker is not None or self.advice_worker is not None or self._pending_advice is not None
                or (self.voice_worker is not None and not self.voice_worker.microphone_enabled())):
            return
        self._deferred_screen_advice = None
        self.handle_advice_candidate(advice)

    def handle_advice_delivery(self, advice):
        # A queued callback from cancelled speech must not cancel newer work.
        if advice != self._pending_advice:
            return
        if isinstance(advice, LeagueAnnouncement):
            if (self._closing or self._stopping or not self._advice_enabled
                    or not config.LEAGUE_PROACTIVE_VOICE_ENABLED or advice != self._pending_advice
                    or not advice.is_fresh() or not self.context.league.announcement_current(advice)
                    or not self.context.interaction.allows_advice(advice.captured_at)):
                self._cancel_advice()
                return
            self.chat_panel.append_message("Capri // League", advice.text)
            self.messages.append({"role": "assistant", "content": advice.text, "proactive": True,
                                  "source": "league_visual" if advice.event_key.startswith("visual:") else "league_api",
                                  "evidence": advice.text, "captured_at": advice.captured_at})
            self.advice_bubble.show_tip(advice.text)
            return
        if (
            self._closing or self._stopping or not self._advice_enabled
            or (self._pending_action is not None and self._pending_action[0] != "voice") or advice != self._pending_advice
            or not advice.is_fresh() or not self.context.is_screen_source_current(advice.source)
            or not self.context.interaction.allows_advice(advice.captured_at)
            or self.context.interaction.repeats_recent_response(advice.text)
        ):
            self._cancel_advice()
            return
        self._advice_policy.record_delivery(advice)
        self.messages.append({
            "role": "assistant",
            "content": advice.text, "proactive": True, "source": "screen",
            "evidence": advice.evidence, "captured_at": advice.captured_at,
        })
        self.chat_panel.append_message("Capri // screen tip", advice.text)
        self.advice_bubble.show_tip(advice.text)

    def handle_advice_finished(self, advice):
        if isinstance(advice,LeagueAnnouncement):self._league_voice_policy.finish(advice)
        if advice == self._pending_advice:
            self._pending_advice = None

    def handle_advice_started(self,advice):
        if isinstance(advice,LeagueAnnouncement) and self._league_voice_policy.inflight==advice:
            self._league_voice_policy.started(advice)

    def handle_advice_error(self, error):
        # Passive advice must never raise/activate chat over the user's game.
        self.chat_panel.append_message("Status", f"Screen tip speech unavailable: {error}")

    def _open_league_calibration(self):
        if not self._closing:self.league_visual_service.open_calibration()

    def toggle_vision(self):
        if self._closing:
            return
        if self.vision_worker is None:
            self.start_vision()
        else:
            self.stop_vision()

    def start_vision(self):
        if self.vision_worker is not None or self._closing:
            return
        self._vision_error = None
        self._reported_vision_errors.clear()
        self.context.clear_screen_observation()
        self.vision_worker = VisionWorker(
            VISION_INTERVAL_SECONDS, VISION_MODEL,
            VISION_MAX_IMAGE_SIZE, VISION_CHANGE_THRESHOLD, follow_foreground=True,
        )
        self.vision_worker.set_monitor_manager(self.monitor_manager)
        self.vision_worker.set_league_client(self.context.league)
        self.context.set_screen_reader(self.vision_worker)
        self.vision_worker.set_advice_enabled(self._advice_enabled)
        self.vision_worker.observation_ready.connect(self.handle_observation)
        self.vision_worker.advice_ready.connect(self.handle_advice_candidate)
        self.vision_worker.status_changed.connect(self.handle_vision_status)
        self.vision_worker.failure.connect(self.handle_vision_failure)
        self.vision_worker.recovered.connect(self.handle_vision_recovery)
        self.vision_worker.finished.connect(self.vision_worker_finished)
        self.vision_button.setChecked(True)
        self._update_vision_geometry()
        self._sync_vision()
        self.vision_worker.start()
        self._sync_controls()
        self._render_status()

    def handle_observation(self, observation, captured_at=None):
        # Ignore queued observations arriving after the user switched vision off.
        if self.vision_worker is not None and not self.vision_worker.isInterruptionRequested():
            if not self._current_vision_signal():
                return
            if self.sender() is self.vision_worker and self.vision_worker.failure_state().get("analysis"):
                return
            self.context.set_screen_observation(observation, captured_at)

    def _current_vision_signal(self):
        # A stopped reader may still have signals queued when a new one starts.
        return self.sender() is None or self.sender() is self.vision_worker

    def _sync_vision(self):
        if self.vision_worker is None or self.vision_worker.isInterruptionRequested():
            return
        self.vision_worker.set_paused(self._inference_busy)
        live = self.vision_worker.live_status()
        if not self._vision_error:
            self.vision_button.setText("VISION LIVE" if live.get("capturing") else
                                      ("VISION PAUSED" if self._inference_busy else "VISION ON"))
        else:
            self.vision_button.setToolTip(f"Screen awareness will retry: {self._vision_error}")
            return
        if live.get("capturing"):
            self.vision_button.setToolTip(
                f"Live capture: {live['capture_fps']:.1f} FPS. Latest frame: {live['frame_age_seconds']:.2f}s ago. "
                + ("Capture continues; background interpretation waits while Capri answers."
                   if self._inference_busy else "Capri compares recent frames for changes. Interpretation is slower than capture."))
            return
        self.vision_button.setToolTip(
            "Paused while Capri processes your request."
            if self._inference_busy else
            "Screen awareness is on. Images are analyzed locally and never saved."
        )

    def stop_vision(self):
        if self.vision_worker is None:
            return
        self.context.set_screen_reader(None)
        self.context.spatial.cancel()
        self.movement_coordinator.reset()
        self._vision_error = None
        self._cancel_advice()
        self.vision_button.setText("VISION STOPPING")
        self.vision_button.setEnabled(False)
        self.vision_worker.stop()
        self._sync_controls()
        self._render_status()

    def handle_vision_status(self, status):
        if self.vision_worker is None or self.vision_worker.isInterruptionRequested() or not self._current_vision_signal():
            return
        self._vision_error = self.context.get_screen_status()["error"] or None
        self._sync_vision()
        self._render_status()
        if not self._vision_error:
            self.vision_button.setToolTip(f"{status}. Screen images stay local and are not saved.")

    def handle_vision_failure(self, stage, error):
        if not self._current_vision_signal():
            return
        if self.sender() is self.vision_worker and self.vision_worker.failure_state().get(stage) != error:
            return  # A newer successful read already recovered this stage.
        self.handle_vision_error(error, stage)

    def handle_vision_recovery(self, stage):
        if self.vision_worker is None or self.vision_worker.isInterruptionRequested() or not self._current_vision_signal():
            return
        if self.sender() is self.vision_worker and self.vision_worker.failure_state().get(stage):
            return
        self.context.clear_screen_error(stage)
        self._reported_vision_errors.pop(stage, None)
        self._vision_error = self.context.get_screen_status()["error"] or None
        self._sync_vision()
        self._render_status()

    def handle_vision_error(self, error, stage="analysis"):
        if self.vision_worker is None or self.vision_worker.isInterruptionRequested():
            return
        self.context.set_screen_error(error, stage)
        self._vision_error = self.context.get_screen_status()["error"] or str(error)
        if self._reported_vision_errors.get(stage) != str(error):
            self._reported_vision_errors[stage] = str(error)
            self.chat_panel.append_message("Vision", f"{stage.capitalize()} failed: {error}")
        self.vision_button.setText("VISION ERROR")
        self._render_status()
        self.vision_button.setToolTip(f"Screen awareness will retry: {self._vision_error}")

    def vision_worker_finished(self):
        worker = self.vision_worker
        self.vision_worker = None
        if worker is not None:
            worker.deleteLater()
        self.context.set_screen_reader(None)
        self._cancel_advice()
        self.vision_button.setEnabled(True)
        self.vision_button.setChecked(False)
        self.vision_button.setText("VISION OFF")
        self.vision_button.setToolTip("Screen awareness is off. Images stay local and are not saved.")
        if self._vision_error and not self._closing:
            self.vision_button.setText("VISION ERROR")
            self.vision_button.setToolTip(f"Screen reading stopped: {self._vision_error}")
            self.chat_panel.append_message("Status", f"Screen reading stopped: {self._vision_error}")
        self._sync_controls()
        self._render_status()
        self._finish_close_if_ready()

    def _active_screen(self):
        center = self.frameGeometry().center()
        return (
            QGuiApplication.screenAt(center)
            or self.screen()
            or QGuiApplication.primaryScreen()
        )

    def _clamp_position(self, x, y):
        point = clamp_to_displays(
            QPoint(int(x), int(y)), self.size(),
            [screen.availableGeometry() for screen in QGuiApplication.screens()],
        )
        return point.x(), point.y()

    def _on_monitors_changed(self):
        self.context.spatial.cancel()
        self.movement_coordinator.reset()
        self.context.clear_screen_observation()
        if self._league_mode_active and config.LEAGUE_DOCK_RIGHT_ENABLED:
            self._dock_league(effects=False)
            self._movement_target = self.pos()
            self._update_vision_geometry()
            return
        # Qt may already have moved a window when its screen disappears. Clamp
        # again using the current topology, without teleporting onto a dead screen.
        x, y = self._clamp_position(self.x(), self.y())
        self.teleport_to(x, y, effects=False)
        self._movement_target = self.pos()
        self._update_vision_geometry()

    def move(self, *args):
        """Existing programmatic moves also snap; drag/startup use native movement."""
        if getattr(self, "_league_mode_active", False) and self.movement_locked:
            return
        effects = getattr(self, "teleport_effects", None)
        if (
            effects is not None and effects.settings.enabled and self.isVisible()
            and self._drag_offset is None and not self._closing
        ):
            self.teleport_to(*args)
        else:
            super().move(*args)

    def _sprite_screen_rect(self):
        target = self.sprite.render_target()
        origin = self.sprite.mapToGlobal(QPoint(0, 0))
        return target.translated(QPointF(origin))

    def teleport_to(self, x, y=None, *, effects=True, arrival_pose=None, league_anchor=False):
        """Move synchronously before constructing/showing any electrical effects."""
        if self.movement_locked and not league_anchor:
            return False
        position = x.toPoint() if isinstance(x, QPointF) else QPoint(x) if y is None else QPoint(int(x), int(y))
        if league_anchor:
            monitor = self._league_monitor()
            if not self._league_mode_active or monitor is None:
                return False
            # Only the transparent window padding may extend outside the work
            # area. The shared visible character envelope stays fully inside.
            visible = self._league_visible_in_window()
            bounds = monitor.available
            position = QPoint(round(max(bounds.x - visible.left(), min(position.x(), bounds.right - visible.right()))),
                              round(max(bounds.y - visible.top(), min(position.y(), bounds.bottom - visible.bottom()))))
        else:
            position = QPoint(*self._clamp_position(position.x(), position.y()))
        if position == self.pos():
            self._movement_target = QPoint(position)
            if arrival_pose is not None:
                arrival_pose()
            return False
        departure = self._sprite_screen_rect()
        silhouette = self.sprite.teleport_silhouette()
        super().move(position)
        if arrival_pose is not None:
            arrival_pose()
        self._movement_target = QPoint(position)
        self.sprite.update()
        if effects and self.isVisible() and self._drag_offset is None and not self._closing:
            try:
                if arrival_pose is None:
                    self.teleport_effects.play(departure, self._sprite_screen_rect(), silhouette)
                else:
                    self.teleport_effects.play(departure, self._sprite_screen_rect(), silhouette,
                                               self.sprite.teleport_silhouette())
            except Exception:
                logging.exception("Teleport VFX failed; Capri has already moved")
                self.teleport_effects.stop()
        return True

    def teleport_randomly(self):
        if self.movement_locked or self._closing or self._drag_offset is not None or self.movement_coordinator.busy:
            return
        self._choose_movement_target()
        self.teleport_to(self._movement_target)

    def _set_movement_state(self, state):
        if self.movement_locked:
            self._movement_state = state
            return
        if state == self._movement_state:
            return

        self._movement_state = state
        if state == "processing":
            self._glitch_steps = 5
            self._next_processing_teleport_at = time.monotonic()
            self._choose_movement_target(100)
        elif state in ("idle", "playful", "happy"):
            self._next_wander_at = time.monotonic() + random.uniform(1, 3)

    def _choose_movement_target(self, radius=None):
        if self.movement_locked:
            self._movement_target = self.pos()
            return
        screen = self._active_screen()
        if screen is None:
            self._movement_target = self.pos()
            return

        bounds = screen.availableGeometry()
        min_x, min_y = bounds.left(), bounds.top()
        max_x = max(min_x, bounds.right() - self.width() + 1)
        max_y = max(min_y, bounds.bottom() - self.height() + 1)
        if radius is None:
            x = random.randint(min_x, max_x)
            y = random.randint(min_y, max_y)
        else:
            x = random.randint(
                max(min_x, self.x() - radius),
                min(max_x, self.x() + radius),
            )
            y = random.randint(
                max(min_y, self.y() - radius),
                min(max_y, self.y() + radius),
            )
        self._movement_target = self.pos()
        self._movement_target.setX(x)
        self._movement_target.setY(y)

    def _move_pet(self):
        if (
            self.movement_locked or self._drag_offset is not None or self.chat_panel.isVisible()
            or self.controls_popup.isVisible()
            or self._inference_busy or self.movement_coordinator.busy
            or self._conversation_worker() is not None
            or not config.AUTONOMOUS_MOVEMENT_ENABLED
            or self._movement_state in ("listening", "speaking", "speech_detected")
        ):
            return

        now = time.monotonic()
        state = self._movement_state
        if self.teleport_effects.settings.enabled:
            if state == "processing":
                if now < self._next_processing_teleport_at:
                    return
                if self._movement_target == self.pos():
                    self._choose_movement_target(100)
                self.teleport_to(self._movement_target)
                self._glitch_steps = self._glitch_steps - 1 if self._glitch_steps > 1 else 5
                # A beat between processing jumps prevents a 30-ms VFX storm.
                self._next_processing_teleport_at = now + 0.45
            elif now >= self._next_wander_at:
                interval = 0.8 if state in ("playful", "happy") else 3.0
                self._choose_movement_target()
                self.teleport_to(self._movement_target)
                self._next_wander_at = now + random.uniform(interval, interval + 3)
            return

        # Explicitly disabling teleport preserves the original autonomous motion.
        if state == "processing":
            if self._glitch_steps <= 0:
                self._choose_movement_target(100)
                self._glitch_steps = 5
            speed = 0.35
        else:
            interval = 0.8 if state in ("playful", "happy") else 3.0
            speed = 0.07 if interval < 1 else 0.025
            if now >= self._next_wander_at:
                self._choose_movement_target()
                self._next_wander_at = now + random.uniform(interval, interval + 3)

        target_x, target_y = self._movement_target.x(), self._movement_target.y()
        new_x = self.x() + (target_x - self.x()) * speed
        new_y = self.y() + (target_y - self.y()) * speed
        x, y = self._clamp_position(new_x, new_y)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

        if abs(target_x - self.x()) < 2 and abs(target_y - self.y()) < 2:
            self._movement_target = self.pos()
            if state == "processing":
                self._glitch_steps -= 1

    def _update_vision_geometry(self):
        if self.vision_worker is None:
            return
        screen = self._active_screen()
        if screen is None:
            return
        bounds = screen.geometry()
        widgets = [self]
        if hasattr(self, "chat_panel") and self.chat_panel.isVisible():
            widgets.append(self.chat_panel)
        if self.controls_popup.isVisible():
            widgets.append(self.controls_popup)
        if hasattr(self, "advice_bubble") and self.advice_bubble.isVisible():
            widgets.append(self.advice_bubble)
        if self.teleport_effects is not None:
            widgets.extend(self.teleport_effects.capture_windows)
        if hasattr(self, "movement_coordinator"):
            widgets.extend(self.movement_coordinator.highlight.capture_windows)
        overlays = []
        for widget in widgets:
            rect = widget.frameGeometry()
            overlays.append((rect.left(), rect.top(), rect.width(), rect.height()))
        self.vision_worker.set_capture_geometry(
            (bounds.left(), bounds.top(), bounds.width(), bounds.height()), overlays,
            [int(widget.winId()) for widget in widgets],
        )

    def moveEvent(self, event):
        self._update_vision_geometry()
        super().moveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "_league_mode_active", False) and hasattr(self, "monitor_manager"):
            self._dock_league(effects=False)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.globalPosition().toPoint()
            self._drag_offset = self._press_position - self.frameGeometry().topLeft()
            self._dragging = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.show_exit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.movement_locked:
            event.accept()
            return
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            position = event.globalPosition().toPoint()
            if (position - self._press_position).manhattanLength() >= QApplication.startDragDistance():
                self._dragging = True
            if self._dragging:
                if self.movement_coordinator.busy:
                    self.context.spatial.cancel()
                    self.movement_coordinator.reset()
                self.controls_popup.hide()
                target = position - self._drag_offset
                self.move(*self._clamp_position(target.x(), target.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press_position is not None:
            position = event.globalPosition().toPoint()
            is_click = (
                not self._dragging
                and (position - self._press_position).manhattanLength() < QApplication.startDragDistance()
            )
            self._dragging = False
            self._drag_offset = None
            self._press_position = None
            self._movement_target = self.pos()
            self._next_wander_at = time.monotonic() + 2
            if is_click:
                self.toggle_controls(position)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.stop_current_turn()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self._closing = True
        self.replay_service.close()
        self.league_visual_service.close()
        self._tts_active = False
        self.sprite.set_league_speaking(False)
        self._live_status_timer.stop()
        self._league_notice_timer.stop()
        self._deferred_screen_advice = None
        self._league_voice_policy.pending = None
        self.context.spatial.cancel()
        self.movement_coordinator.reset()
        self._movement_timer.stop()
        self.sprite.stop_animation()
        self.teleport_effects.stop()
        self.controls_popup.hide()
        self.advice_bubble.hide()
        workers = (self.voice_worker, self.chat_worker, self.advice_worker, self.vision_worker,
                   self.voice_warmup_worker, self.league_worker, self.replay_service.worker,self.league_visual_service.worker,
                   self.league_visual_service.recording_worker)
        if any(worker is not None and worker.isRunning() for worker in workers):
            self._closing = True
            self._movement_timer.stop()
            self.stop_current_turn()
            if self.vision_worker is not None:
                self.stop_vision()
            if self.voice_warmup_worker is not None:
                self.voice_warmup_worker.stop()
            if self.league_worker is not None:
                self.league_worker.stop()
            event.ignore()
            return
        self.chat_panel.close()
        self.context.league.knowledge.close()
        self.context.league_replay = None
        self.context.league_visual = None
        self.teleport_effects.dispose()
        self.movement_coordinator.dispose()
        self.monitor_manager.dispose()
        event.accept()

    def _finish_close_if_ready(self):
        if not self._closing:
            return
        if all(worker is None for worker in (self.voice_worker, self.chat_worker, self.advice_worker,
                                            self.vision_worker, self.voice_warmup_worker, self.league_worker,self.replay_service.worker,self.league_visual_service.worker,
                                            self.league_visual_service.recording_worker)):
            self.close()

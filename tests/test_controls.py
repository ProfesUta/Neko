from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.context import Context
from gui.styles import DARK_THEME
from gui.window import CapriWindow


def make_window(qapp):
    qapp.setStyleSheet(DARK_THEME)
    window = CapriWindow([{"role": "system", "content": "You are Capri."}], Context())
    window.show()
    window._movement_timer.stop()
    qapp.processEvents()
    return window


def click_character(window, button=Qt.MouseButton.LeftButton):
    QTest.mouseClick(window, button, pos=QPoint(100, 100))


def test_controls_start_hidden_and_left_click_toggles_them(qapp):
    window = make_window(qapp)
    try:
        assert not window.controls_popup.isVisible()
        assert not window.voice_button.isVisible()
        assert not window.close_button.isVisible()
        click_character(window)
        assert window.controls_popup.isVisible()
        assert window.voice_button.isVisible()
        assert window.vision_button.isVisible()
        assert window.chat_button.isVisible()
        assert not window.close_button.isVisible()
        click_character(window)
        assert not window.controls_popup.isVisible()
    finally:
        window.close()


def test_using_control_hides_popup_and_still_performs_action(qapp):
    window = make_window(qapp)
    try:
        click_character(window)
        QTest.mouseClick(window.chat_button, Qt.MouseButton.LeftButton)
        assert not window.controls_popup.isVisible()
        assert window.chat_panel.isVisible()
    finally:
        window.close()


def test_click_in_chat_dismisses_controls_without_swallowing_click(qapp):
    window = make_window(qapp)
    try:
        window.show_chat()
        click_character(window)
        assert window.controls_popup.isVisible()
        presses = []
        class ClickObserver(QObject):
            def eventFilter(self, watched, event):
                if event.type() == QEvent.Type.MouseButtonPress:
                    presses.append(event.button())
                return False
        observer = ClickObserver()
        window.chat_panel.input.installEventFilter(observer)
        QTest.mouseClick(window.chat_panel.input, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert not window.controls_popup.isVisible()
        assert presses == [Qt.MouseButton.LeftButton]
    finally:
        window.close()


def test_native_outside_click_dismisses_without_replaying_on_character(qapp):
    window = make_window(qapp)
    try:
        click_character(window)
        popup = window.controls_popup
        # Native popup windows receive grabbed clicks in outside local coordinates.
        owner_point = window.mapToGlobal(QPoint(80, 80))
        outside_point = popup.mapFromGlobal(owner_point)
        QTest.mousePress(popup, Qt.MouseButton.LeftButton, pos=outside_point)
        assert not popup.isVisible()
        assert popup.testAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        QTest.mouseRelease(popup, Qt.MouseButton.LeftButton, pos=outside_point)
        assert not popup.isVisible()
    finally:
        window.close()


def test_right_click_only_shows_exit_and_exit_closes_character(qapp):
    window = make_window(qapp)
    try:
        click_character(window, Qt.MouseButton.RightButton)
        assert window.controls_popup.isVisible()
        assert window.close_button.isVisible()
        assert not window.voice_button.isVisible()
        assert not window.chat_button.isVisible()
        QTest.mouseClick(window.close_button, Qt.MouseButton.LeftButton)
        assert not window.controls_popup.isVisible()
        assert not window.isVisible()
    finally:
        window.close()


def test_right_click_can_replace_open_controls(qapp):
    window = make_window(qapp)
    try:
        click_character(window)
        click_character(window, Qt.MouseButton.RightButton)
        assert window.controls_popup.mode == "exit"
        assert window.close_button.isVisible()
        assert not window.voice_button.isVisible()
    finally:
        window.close()


def test_drag_moves_character_without_opening_controls(qapp):
    window = make_window(qapp)
    try:
        window.move(200, 200)
        origin = window.pos()
        local = QPoint(100, 100)
        global_position = window.mapToGlobal(local)
        delta = QPoint(40, -30)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=local)
        move = QMouseEvent(
            QEvent.Type.MouseMove, QPointF(local + delta),
            QPointF(global_position + delta), Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(window, move)
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=local)
        assert window.pos() == origin + delta
        assert not window.controls_popup.isVisible()
        assert window._drag_offset is None
    finally:
        window.close()


def test_popup_stays_on_screen_and_escape_dismisses_it(qapp):
    window = make_window(qapp)
    try:
        bounds = window.screen().availableGeometry()
        window.toggle_controls(bounds.bottomRight())
        qapp.processEvents()
        assert bounds.contains(window.controls_popup.frameGeometry())
        QTest.keyClick(window.controls_popup, Qt.Key.Key_Escape)
        assert not window.controls_popup.isVisible()
    finally:
        window.close()


def test_grabbed_right_click_on_character_switches_to_exit(qapp):
    window = make_window(qapp)
    try:
        click_character(window)
        popup = window.controls_popup
        owner_point = window.mapToGlobal(QPoint(80, 80))
        outside_point = popup.mapFromGlobal(owner_point)
        QTest.mousePress(popup, Qt.MouseButton.RightButton, pos=outside_point)
        assert popup.isVisible()
        assert popup.mode == "exit"
        assert window.close_button.isVisible()
        QTest.mouseRelease(popup, Qt.MouseButton.RightButton)
    finally:
        window.close()


def test_application_deactivation_hides_open_controls(qapp):
    window = make_window(qapp)
    try:
        click_character(window)
        QApplication.sendEvent(qapp, QEvent(QEvent.Type.ApplicationDeactivate))
        assert not window.controls_popup.isVisible()
    finally:
        window.close()


def test_popup_background_click_dismisses_controls(qapp):
    window = make_window(qapp)
    try:
        click_character(window)
        QTest.mouseClick(window.controls_popup, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
        assert not window.controls_popup.isVisible()
    finally:
        window.close()

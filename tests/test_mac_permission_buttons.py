import unittest
from unittest.mock import patch

import mac_app


def controller():
    return mac_app.Controller.__new__(mac_app.Controller)


class PermissionButtonTests(unittest.TestCase):
    # macOS shows its own permission prompt only once per app; later requests are silent,
    # so every button must either open System Settings or say the permission is granted.
    def test_screen_menu_says_so_when_already_granted(self):
        app = controller()
        app.window = None
        with patch('macos_support.has_capture_permission', return_value=True), \
                patch('PySide6.QtWidgets.QMessageBox.information') as info, \
                patch.object(app, 'check_permission') as check:
            app.show_capture_permission()
        info.assert_called_once()
        check.assert_not_called()

    def test_screen_menu_falls_back_to_the_request_flow(self):
        app = controller()
        with patch('macos_support.has_capture_permission', return_value=False), \
                patch.object(app, 'check_permission') as check:
            app.show_capture_permission()
        check.assert_called_once()

    def test_repeat_button_opens_the_missing_pane(self):
        for state, pane in (({'inputMonitoring': False, 'accessibility': False}, 'Privacy_ListenEvent'),
                            ({'inputMonitoring': True, 'accessibility': False}, 'Privacy_Accessibility')):
            app = controller()
            app.window = None
            with self.subTest(state=state), patch.object(mac_app.subprocess, 'Popen') as popen, \
                    patch('PySide6.QtWidgets.QMessageBox.information') as info:
                app.open_missing_repeat_permission(state)
                self.assertTrue(popen.call_args[0][0][1].endswith(pane))
                info.assert_called_once()  # visible feedback even if Settings opens behind the game

    def test_repeat_button_says_so_when_everything_is_granted(self):
        app = controller()
        app.window = None
        with patch.object(mac_app.subprocess, 'Popen') as popen, \
                patch('PySide6.QtWidgets.QMessageBox.information') as info:
            app.open_missing_repeat_permission({'inputMonitoring': True, 'accessibility': True})
        popen.assert_not_called()
        info.assert_called_once()


if __name__ == '__main__':
    unittest.main()

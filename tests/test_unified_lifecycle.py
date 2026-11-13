"""Independent UI lifecycle checks, with no permissions or real input capture."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
import tracker
from mac_app import Controller


class UnifiedLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base_patch = patch.object(tracker, 'BASE', Path(self.directory.name))
        self.base_patch.start()
        self.controller = Controller(self.application, start_worker=False, restore=False)
        self.application.processEvents()

    def tearDown(self):
        self.controller.key_repeat.shutdown()
        self.controller.stop.set()
        self.controller.timer.stop()
        self.controller.start_captures.close()
        self.controller.window.hide()
        self.controller.window.deleteLater()
        self.application.processEvents()
        self.base_patch.stop()
        self.directory.cleanup()

    def test_diagnostics_controller_does_not_start_native_helper_or_capture(self):
        self.assertEqual(self.controller.key_repeat.process.state(), QProcess.ProcessState.NotRunning)
        self.assertFalse(self.controller.running)
        self.assertFalse(self.controller.key_repeat.state['enabled'])
        self.assertFalse(self.controller.worker_ready)

    def test_measurement_button_does_not_enable_key_repetition(self):
        self.controller.window_region = dict(left=.1, top=.2, width=.3, height=.1, reference_aspect=1.7)
        with patch.object(self.controller, 'check_permission', return_value=True), \
                patch.object(self.controller.key_repeat, 'set_enabled') as repeat:
            self.controller.toggle()
            self.assertTrue(self.controller.running)
            repeat.assert_not_called()

    def test_repeat_button_does_not_start_or_reset_experience_measurement(self):
        original = self.controller.session
        with patch.object(self.controller.key_repeat, 'set_enabled', return_value=True) as enable:
            self.controller.toggle_repeat()
            enable.assert_called_once_with(True)
        self.assertFalse(self.controller.running)
        self.assertIs(self.controller.session, original)

    def test_failed_final_experience_save_still_disables_repeat_and_keeps_window(self):
        with patch.object(self.controller, 'finish_measurement', return_value=False), \
                patch.object(self.controller.key_repeat, 'shutdown') as stop:
            self.assertFalse(self.controller.shutdown())
            stop.assert_called_once()
        self.assertFalse(self.controller.stop.is_set())
        self.assertTrue(self.controller.timer.isActive())
        self.assertTrue(self.controller.start_captures.thread.is_alive())

    def test_successful_shutdown_closes_capture_writer_timer_and_repeat(self):
        self.assertTrue(self.controller.shutdown())
        self.assertTrue(self.controller.key_repeat.closing)
        self.assertTrue(self.controller.stop.is_set())
        self.assertFalse(self.controller.timer.isActive())
        self.assertFalse(self.controller.start_captures.thread.is_alive())
        self.assertEqual(self.controller.key_repeat.process.state(), QProcess.ProcessState.NotRunning)


if __name__ == '__main__':
    unittest.main()

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from PySide6.QtWidgets import QApplication
import tracker
from auto_hud import AutomaticCapture, Confirmation, Hud, HudLevelChanged, HudNotFound
from mac_app import Controller


def hud(**changes):
    roi = dict(left=.40, top=.95, width=.14, height=.035, reference_aspect=1.6)
    level_roi = dict(left=.035, top=.95, width=.04, height=.03, reference_aspect=1.6)
    return replace(Hud(170, (34514347, 8.06), roi, level_roi, 100, (1600, 1000)), **changes)


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_resets_for_level_window_layout_decrease_and_long_gap(self):
        for other, stamp in [(hud(level=171), 3), (hud(window_id=101), 3),
                             (hud(size=(1800, 1000)), 3), (hud(value=(1, .01)), 3),
                             (hud(exp_roi=dict(hud().exp_roi, left=.7)), 3), (hud(), 20)]:
            gate = Confirmation()
            self.assertFalse(gate.accept(hud(), 1))
            self.assertFalse(gate.accept(hud(), 2))
            self.assertFalse(gate.accept(other, stamp))
            self.assertEqual(gate.count, 1)

    def test_monotonic_hunting_gain_can_be_confirmed(self):
        gate = Confirmation()
        self.assertFalse(gate.accept(hud(), 1))
        self.assertFalse(gate.accept(hud(value=(34514348, 8.06)), 2))
        self.assertTrue(gate.accept(hud(value=(34514349, 8.06)), 3))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new('RGB', (1600, 1000))
        self.image.info['window_id'] = 100
        self.client = Mock()
        self.client.snapshot.return_value = self.image
        self.locator = Mock()
        self.locator.locate.return_value = hud()
        self.now = 1
        self.capture = AutomaticCapture(self.client, self.locator, lambda: self.now)

    def confirmed(self, generation=1, level=169, baseline=False):
        for n in (1, 2):
            self.now = n
            with self.assertRaises(HudNotFound):
                self.capture.read(generation, level, baseline, True)
        self.now = 3
        return self.capture.read(generation, level, baseline, True)

    def test_no_saved_region_is_needed_and_new_generation_must_confirm_again(self):
        image = self.confirmed()
        self.assertEqual(image.info['detected_level'], 170)
        self.assertIs(image.info['full_game_image'], self.image)
        self.now = 4
        self.capture.read(1, 170, True, False)
        self.client.snapshot.assert_called_with(roi=hud().exp_roi, include_full=False)
        with self.assertRaises(HudNotFound):
            self.capture.read(2, 170, False, True)
        self.assertEqual(self.capture.gate.count, 1)

    def test_confirmed_level_change_stops_existing_session(self):
        with self.assertRaises(HudLevelChanged):
            self.confirmed(level=169, baseline=True)

    def test_hidden_hud_resets_confirmation_and_cannot_reuse_previous_reads(self):
        self.now = 1
        with self.assertRaises(HudNotFound): self.capture.read(1, 169, False, True)
        self.locator.locate.side_effect = HudNotFound('covered')
        self.now = 2
        with self.assertRaises(HudNotFound): self.capture.read(1, 169, False, True)
        self.locator.locate.side_effect = None
        self.now = 3
        with self.assertRaises(HudNotFound): self.capture.read(1, 169, False, True)
        self.assertEqual(self.capture.gate.count, 1)

    def test_another_window_cannot_supply_values_under_cached_region(self):
        self.confirmed()
        self.image.info['window_id'] = 999
        self.now = 4
        with self.assertRaises(HudNotFound): self.capture.read(1, 170, True, False)
        self.assertIsNone(self.capture.hud)

    def test_aspect_change_restarts_discovery(self):
        self.confirmed()
        self.client.snapshot.side_effect = ValueError('aspect')
        self.now = 4
        with self.assertRaises(HudNotFound): self.capture.read(1, 170, True, False)
        self.assertIsNone(self.capture.hud)

    def test_resize_at_same_aspect_restarts_discovery(self):
        self.image.info['content_points'] = (1600, 1000)
        self.confirmed()
        self.image.info['content_points'] = (1280, 800)
        self.now = 4
        with self.assertRaises(HudNotFound):
            self.capture.read(1, 170, True, False)
        self.assertIsNone(self.capture.hud)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = patch.object(tracker, 'BASE', Path(self.temp.name)); self.base.start()
        self.c = Controller(self.app, start_worker=False, restore=False)
        self.c.timer.stop()
    def tearDown(self):
        self.c.key_repeat.shutdown(); self.c.start_captures.close()
        self.c.window.hide(); self.c.window.deleteLater(); self.app.processEvents()
        self.base.stop(); self.temp.cleanup()

    def test_start_uses_automatic_discovery_without_opening_selection(self):
        with patch.object(self.c, 'check_permission', return_value=True), patch.object(self.c, 'select') as select:
            self.c.toggle()
        select.assert_not_called()
        self.assertTrue(self.c.running)
        self.assertIsNone(self.c.region)
        self.assertIsNone(self.c.window_region)

    def test_detected_level_replaces_default_before_starting_baseline(self):
        c = self.c; c.running = True
        for stamp in (1, 2):
            c.events.put((c.generation, (34514347, 8.06), 'fixture', .99, None, stamp,
                          dict(detected_level=170, auto_detected=True, temporal={'selected':1})))
            c.poll()
        self.assertEqual(c.session.level, 170)
        self.assertEqual(c.session.total, tracker.EXP_TABLE[170])
        self.assertEqual(c.session.first, (34514347, 8.06))
        self.assertIsNone(c.window_region)
        self.assertEqual(c.level_var.get(), 170)

    def test_obsolete_discovery_event_cannot_change_new_session(self):
        c = self.c; c.running = True
        c.events.put((c.generation-1, (34514347, 8.06), 'fixture', .99, None, 1,
                      dict(detected_level=170, auto_detected=True)))
        c.poll()
        self.assertEqual(c.session.level, 169)
        self.assertIsNone(c.session.first)

    def test_verified_automatic_capture_can_start_during_active_hunting(self):
        c = self.c; c.running = True
        c.events.put((c.generation, (34514347, 8.06), 'fixture', .99, None, 1,
                      dict(detected_level=170, auto_detected=True, auto_confirmed=True,
                           temporal={'selected':1})))
        c.poll()
        self.assertEqual(c.session.first, (34514347, 8.06))
        self.assertIsNone(c.session.started)
        c.events.put((c.generation, (34515347, 8.06), 'fixture', .99, None, 2,
                      dict(detected_level=170, auto_detected=True, auto_confirmed=True,
                           temporal={'selected':1})))
        c.poll()
        self.assertEqual(c.session.stats(2)[0], 1000)
        self.assertEqual(c.session.first, (34514347, 8.06))

    def test_automatic_location_without_temporal_agreement_cannot_commit_baseline(self):
        c = self.c; c.running = True
        c.events.put((c.generation, (34514347, 8.06), 'fixture', .99, None, 1,
                      dict(detected_level=170, auto_detected=True, auto_confirmed=True,
                           temporal={'selected':None})))
        c.poll()
        self.assertIsNone(c.session.first)

    def test_confirmed_level_change_preserves_total_and_stops(self):
        c = self.c; c.session = tracker.Session(level=170); c.level_var.set(170)
        c.session.accept((34514347, 8.06), 1); c.session.accept((34514347, 8.06), 2)
        c.session.accept((34515347, 8.06), 3); c.running = True
        total = c.session.stats(3)[0]
        c.events.put((c.generation, None, '', 0, '레벨 변경', 4, dict(level_changed=True)))
        c.poll()
        self.assertFalse(c.running)
        self.assertEqual(c.session.stats(4)[0], total)
        self.assertEqual(c.session.level, 170)

    def test_manual_mode_still_uses_selection_and_settings_round_trip(self):
        c = self.c
        c.auto_checkbox.setChecked(False)
        self.assertFalse(c.auto_hud)
        self.assertTrue(c.level_picker.isEnabled())
        settings, warning = tracker.load_settings(tracker.BASE/'settings.json')
        self.assertFalse(settings['auto_hud'])
        self.assertIsNone(warning)
        with patch.object(c, 'select') as select:
            c.toggle()
        select.assert_called_once()

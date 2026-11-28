"""Tests for v1.4.43 seamless invisible JEV integration & shared Linux NativeOcr potion OCR."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
from PySide6.QtWidgets import QApplication

import jev
import jev_assist
import native_ocr
import potions
import tracker
from mac_app import Controller


class SeamlessJevTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_controller(self, tmp):
        c = Controller(self.app, start_worker=False, restore=False)
        c.timer.stop()
        c.jev_arbiter = jev.Arbiter(lambda: c.jev_enabled, cooldown=0, runner=lambda w: w())
        c.potion_reader = potions.Reader(arbiter=c.jev_arbiter)
        return c

    def _cleanup(self, c):
        c.key_repeat.shutdown()
        c.start_captures.close()
        c._shutdown_probes()
        c.window.hide()
        c.window.deleteLater()
        self.app.processEvents()

    def test_potion_registration_quietly_prefills_name_and_shop_price_when_jev_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.jev_enabled = True
                roi = {'left': 0.85, 'top': 0.85, 'width': 0.05, 'height': 0.05, 'reference_aspect': 16 / 9}
                shot = Image.new('RGB', (1000, 600), (30, 40, 50))
                shot.info['content_points'] = (1000, 600)
                with patch('jev_assist.ask_potion_preset', return_value=('쭈쭈바', 2300)) as preset, \
                        patch('PySide6.QtWidgets.QInputDialog.getText', return_value=('쭈쭈바', True)) as get_text, \
                        patch('PySide6.QtWidgets.QInputDialog.getInt', return_value=(2300, True)) as get_int:
                    c.apply_potion_slot(roi, selection_image=shot)
                preset.assert_called_once()
                self.assertEqual(get_text.call_args.kwargs['text'], '쭈쭈바')
                self.assertEqual(get_int.call_args.args[3], 2300)
                self.assertEqual(c.potion_slots[0]['name'], '쭈쭈바')
                self.assertEqual(c.potion_slots[0]['unit_cost'], 2300)
            finally:
                self._cleanup(c)

    def test_meso_drop_near_potion_restock_suggests_potion_purchase_with_zero_jev_calls(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.jev_enabled = True
                c.running = True
                c.session.potions.observe('쭈쭈바', 100, unit_cost=2300, stamp=10.0)
                c.session.potions.observe('쭈쭈바', 500, unit_cost=2300, stamp=105.0)  # restock at t=105
                c.meso_change_stamps = {(c.session.session_id, 0): 110.0}
                with patch('jev_assist.ask_meso_change') as ask:
                    hint = c.meso_change_suggestion(0, -920_000)
                self.assertEqual(hint, '물약 구매')
                ask.assert_not_called()
            finally:
                self._cleanup(c)

    def test_npc_loot_sale_or_hunting_meso_jump_silently_hides_prompt_without_nagging(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.jev_enabled = True
                c.running = True
                c.session.meso.observe(5_000_000, active_seconds=0.0)
                c.session.meso.observe(7_500_000, threshold=500_000, active_seconds=60.0)
                c.jev_frames.remember(60.0, Image.new('RGB', (400, 300)), force=True)
                with patch('jev.ask', return_value='2'):  # choice 2 = NPC loot sale -> '사냥 획득'
                    c.update_meso_change_prompt()
                    c.update_meso_change_prompt()
                self.assertTrue(c.meso_change_prompt.isHidden())
                self.assertEqual(c.session.meso.marked, set())
            finally:
                self._cleanup(c)

    def test_local_buff_interval_explains_efficiency_change_without_calling_jev(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.jev_enabled = True
                c.running = True
                c.session.started = 0.0
                c.session.exp_buff_intervals = {'coupon': [[0.0, 355.0]]}  # ended near minute 6 (360s)
                with patch('jev_assist.ask_change') as ask:
                    tag = c.change_suggestion(dict(minute=6, before=1000.0, after=500.0))
                self.assertEqual(tag, '경험치 버프 변화')
                ask.assert_not_called()
            finally:
                self._cleanup(c)

    def test_potion_suspicious_drop_disagreement_uses_jev_tie_break(self):
        arbiter = jev.Arbiter(lambda: True, cooldown=0, runner=lambda w: w())
        crop = Image.new('RGB', (44, 22), (40, 40, 40))
        responses = iter([[('25', 0.91)], [('250', 0.93)], [('25', 0.91)], [('250', 0.93)]])
        with patch('jev.pick_reading', return_value=1) as pick:
            # First call submits tie-break to arbiter and returns None while pending.
            first = potions.recognize_count(crop, ocr_fn=lambda _: next(responses), arbiter=arbiter, prev_value=251)
            self.assertIsNone(first)
            pick.assert_called_once()
            self.assertEqual(pick.call_args.args[1], ['25', '250'])
            # Second call reads the cached JEV pick (250) instead of accepting the digit drop (25)!
            second = potions.recognize_count(crop, ocr_fn=lambda _: next(responses), arbiter=arbiter, prev_value=251)
            self.assertEqual(second, 250)

    def test_map_reading_schedules_one_quiet_retry_when_occluded_at_start(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.running = True
                sid = c.session.session_id
                c.map_read_attempts = (sid, 1)
                c.map_results.put((sid, None, (None, None)))
                with patch.object(c, '_monotonic', return_value=100.0):
                    c.apply_map_results()
                self.assertEqual(c.map_retry_at, (sid, 100.0 + c.MAP_RETRY_DELAY))
            finally:
                self._cleanup(c)

    def test_potions_reuses_shared_linux_native_ocr_engine(self):
        fake_native = native_ocr.NativeOcr(SimpleNamespace(
            text=lambda b, w, h: ('557', 0.98),
            experience=lambda b, w, h, t: (None, 0.0, '', []),
        ))
        prev = tracker.shared_ocr_engine()
        tracker.register_shared_engine(fake_native)
        try:
            eng, lock = potions._get_rec_engine()
            self.assertIs(eng, fake_native)
            self.assertIs(lock, fake_native.lock)
            self.assertEqual(potions.recognize_count(Image.new('RGB', (48, 24), (30, 30, 30))), 557)
        finally:
            tracker.register_shared_engine(prev)


if __name__ == '__main__':
    unittest.main()

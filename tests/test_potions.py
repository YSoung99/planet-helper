import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

import potions
import record_history
import tracker
from mac_app import Controller


class PotionCoreTests(unittest.TestCase):
    def test_slot_confirmation_requires_two_sightings_and_three_for_large_drop_or_zero(self):
        conf = potions.SlotConfirmation()
        self.assertIsNone(conf.observe(557, 1.0))
        self.assertEqual(conf.observe(557, 3.0), 557)
        # Small normal consumption (-2): 2 sightings
        self.assertIsNone(conf.observe(555, 5.0))
        self.assertEqual(conf.observe(555, 7.0), 555)
        # Sudden drop to 0 or >= 50: needs 3 sightings
        self.assertIsNone(conf.observe(500, 9.0))
        self.assertIsNone(conf.observe(500, 11.0))
        self.assertEqual(conf.observe(500, 13.0), 500)
        self.assertIsNone(conf.observe(0, 15.0))
        self.assertIsNone(conf.observe(0, 17.0))
        self.assertEqual(conf.observe(0, 19.0), 0)

    def test_session_potions_counts_decreases_and_ignores_restocks(self):
        sp = potions.SessionPotions()
        self.assertTrue(sp.observe('쭈쭈바', 557, unit_cost=2300))
        self.assertTrue(sp.observe('쭈쭈바', 540, unit_cost=2300))
        self.assertEqual(sp.total_used(), 17)
        # Restocked from 540 -> 940: used stays 17, restocks becomes 1
        self.assertTrue(sp.observe('쭈쭈바', 940, unit_cost=2300))
        self.assertEqual(sp.total_used(), 17)
        self.assertEqual(sp.items['쭈쭈바']['restocks'], 1)
        # Use 3 more -> total 20
        self.assertTrue(sp.observe('쭈쭈바', 937, unit_cost=2300))
        self.assertEqual(sp.total_used(), 20)
        self.assertEqual(sp.total_cost(), 46_000)
        text = sp.summary_text(active_seconds=1800)
        self.assertIn('쭈쭈바 20개', text)
        self.assertIn('비용 -4.6만', text)
        # Round trip serialization
        restored = potions.SessionPotions(sp.as_dict())
        self.assertEqual(restored.total_used(), 20)
        self.assertEqual(restored.total_cost(), 46_000)

    def test_reader_caches_identical_crops_and_trims_square_slot_crops(self):
        frame = Image.new('RGB', (1000, 600), (30, 40, 50))
        # Square quickslot icon ROI (50x50) -> crop_digit_strip trims to lower 48%
        roi = {'left': 0.85, 'top': 0.85, 'width': 0.05, 'height': 50 / 600, 'reference_aspect': 1000 / 600}
        strip = potions.crop_digit_strip(frame, roi)
        self.assertIsNotNone(strip)
        self.assertLess(strip.height, 30)

        calls = []
        def fake_ocr(img):
            calls.append(img.size)
            return [('322', 0.96)]

        reader = potions.Reader(ocr_fn=fake_ocr)
        slots = [{'name': '엘릭서', 'unit_cost': 0, 'roi': roi}]
        self.assertEqual(reader.read_slots(frame, slots), [('엘릭서', 322)])
        self.assertEqual(reader.read_slots(frame, slots), [('엘릭서', 322)])
        self.assertEqual(len(calls), 1)  # second read skipped OCR via hash cache

        # Pixel change inside the slot invalidates the cache.
        changed = frame.copy()
        ImageDraw.Draw(changed).rectangle((855, 540, 890, 555), fill=(255, 255, 255))
        self.assertEqual(reader.read_slots(changed, slots), [('엘릭서', 322)])
        self.assertEqual(len(calls), 2)

    def test_settings_validation_and_history_tooltip_include_potions(self):
        roi = {'left': 0.85, 'top': 0.88, 'width': 0.04, 'height': 0.04, 'reference_aspect': 16 / 9}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            path.write_text(json.dumps({
                'potion_slots': [
                    {'name': '쭈쭈바', 'unit_cost': 2300, 'roi': roi},
                    {'name': '', 'unit_cost': 100, 'roi': roi},  # invalid
                ]
            }), encoding='utf-8')
            clean, warning = tracker.load_settings(path)
            self.assertIsNone(warning)
            self.assertEqual(len(clean['potion_slots']), 1)
            self.assertEqual(clean['potion_slots'][0]['name'], '쭈쭈바')

        row = dict(
            active=1800, idle=0, gap=0, gap_gain=0, per_minute=5000, blocked=False,
            stop_reason='', changes=[], meso=500_000, meso_trade=0, meso_large_changes=0,
            meso_seconds=1800,
            potions={'items': [{'name': '쭈쭈바', 'used': 50, 'unit_cost': 2300, 'cost': 115_000}],
                     'total_used': 50, 'total_cost': 115_000},
        )
        tip = record_history.row_details(row, '')
        self.assertIn('쭈쭈바 50개', tip)
        self.assertIn('물약비 제외 순수익 +385,000', tip)

    def test_icon_matching_tracks_potion_across_different_hotkeys_and_skips_when_absent(self):
        # Draw a distinctive potion icon (red flask + gold cap in the center)
        def make_icon(hotkey_text, count_text):
            tile = Image.new('RGB', (52, 50), (220, 220, 220))
            d = ImageDraw.Draw(tile)
            d.ellipse((14, 16, 44, 42), fill=(220, 30, 40), outline=(40, 10, 10))
            d.rectangle((24, 6, 34, 18), fill=(240, 180, 40), outline=(50, 30, 10))
            d.text((2, 2), hotkey_text, fill=(255, 255, 255))
            d.text((4, 34), count_text, fill=(255, 255, 255))
            return tile

        with tempfile.TemporaryDirectory() as tmp:
            # Save template taken from hotkey 'Del' with count '557'
            template_tile = make_icon('Del', '557')
            icon_file = potions.save_icon(template_tile, base=tmp)
            slot = {'name': '쭈쭈바', 'unit_cost': 2300, 'icon': icon_file, 'reference_height': 600}
            self.assertTrue(potions.valid_slot(slot))

            # Place the SAME potion icon on a DIFFERENT position & hotkey ('PgUp', count '412')!
            frame = Image.new('RGB', (1000, 600), (25, 35, 45))
            moved_tile = make_icon('PgUp', '412')
            frame.paste(moved_tile, (820, 520))
            reader = potions.Reader(ocr_fn=lambda img: [('412', 0.96)], base=tmp)
            self.assertEqual(reader.read_slots(frame, [slot]), [('쭈쭈바', 412)])
            # Verify it located the moved coordinates (820, 520), not any fixed ROI.
            self.assertEqual(reader._last_boxes[0][1][:2], (820, 520))

            # If the potion is removed from the quickslot bar, reader returns None cleanly.
            empty = Image.new('RGB', (1000, 600), (25, 35, 45))
            self.assertEqual(reader.read_slots(empty, [slot]), [('쭈쭈바', None)])


class PotionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_controller_registers_tracks_and_displays_potion_usage(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = Controller(self.app, start_worker=False, restore=False)
            c.timer.stop()
            try:
                c.toggle_display()
                self.assertTrue(c.potion_label.isHidden())
                roi = {'left': 0.85, 'top': 0.88, 'width': 0.04, 'height': 0.04, 'reference_aspect': 16 / 9}
                with patch('PySide6.QtWidgets.QInputDialog.getText', return_value=('쭈쭈바', True)), \
                        patch('PySide6.QtWidgets.QInputDialog.getInt', return_value=(2300, True)):
                    c.apply_potion_slot(roi)
                self.assertEqual(len(c.potion_slots), 1)
                self.assertFalse(c.potion_label.isHidden())
                c.running = True
                for stamp, cnt in ((10.0, 557), (12.0, 557), (14.0, 547), (16.0, 547)):
                    c.potion_results.put((c.generation, 20, stamp, [('쭈쭈바', cnt)]))
                    c.apply_potion_results(stamp)
                self.assertEqual(c.session.potions.total_used(), 10)
                self.assertIn('쭈쭈바 10개', c.potion_label.text())
                self.assertIn('비용 -2.3만', c.potion_label.text())
                menu = c.build_context_menu(); self.addCleanup(menu.deleteLater)
                gains = next(a for a in menu.actions() if a.text() == '메소·버프').menu()
                self.assertIn('등록된 포션 관리 (1개)…', [a.text() for a in gains.actions()])
            finally:
                c.key_repeat.shutdown(); c.start_captures.close(); c._shutdown_probes()
                c.window.hide(); c.window.deleteLater(); self.app.processEvents()


if __name__ == '__main__':
    unittest.main()

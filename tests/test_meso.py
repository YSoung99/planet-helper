import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import meso


def inventory_frame(box=(300, 400, 150, 18), trade_box=(60, 200, 120, 18)):
    """A dark game frame with the inventory meso box and a trade window box."""
    frame = Image.new('RGB', (800, 600), (40, 60, 90))
    draw = ImageDraw.Draw(frame)
    for x, y, w, h in (box, trade_box):
        draw.rectangle((x, y, x + w - 1, y + h - 1), fill=(250, 250, 250))
    return frame


class ReaderTests(unittest.TestCase):
    def test_white_boxes_finds_wide_boxes_only(self):
        frame = inventory_frame()
        ImageDraw.Draw(frame).rectangle((500, 100, 540, 140), fill=(255, 255, 255))  # a square, not a box
        self.assertEqual(sorted(meso.white_boxes(frame)), [(60, 200, 120, 18), (300, 400, 150, 18)])

    def test_box_without_the_meso_label_is_ignored(self):
        frame = inventory_frame()
        calls = []

        def label(frame, box):
            calls.append(box)
            return box == (300, 400, 150, 18)
        with mock.patch.object(meso, 'has_label', side_effect=label), \
             mock.patch.object(meso, 'read_amount', return_value=8890080):
            reader = meso.Reader()
            self.assertEqual(reader.read(frame), 8890080)
        self.assertEqual(reader.box, (300, 400, 150, 18))

    def test_remembered_box_is_read_first_and_dropped_when_gone(self):
        reader = meso.Reader()
        reader.box = (1, 2, 3, 4)
        with mock.patch.object(meso, 'read_amount', return_value=5) as read:
            self.assertEqual(reader.read(inventory_frame()), 5)
        read.assert_called_once()
        with mock.patch.object(meso, 'read_amount', return_value=None), \
             mock.patch.object(meso, 'white_boxes', return_value=[]):
            self.assertIsNone(reader.read(inventory_frame()))
        self.assertIsNone(reader.box)

    def test_amount_must_be_a_clean_number(self):
        frame = inventory_frame()
        box = (300, 400, 150, 18)
        for text, expected in (('8,890,080', 8890080), ('0', 0), ('8.890.080', 8890080),
                               ('8,89O,080', None), ('8,890,0', None), ('', None)):
            with mock.patch('map_name.recognize_lines', return_value=[(text, .99)]):
                self.assertEqual(meso.read_amount(frame, box), expected, text)
        with mock.patch('map_name.recognize_lines', return_value=[('8,890,080', .5)]):
            self.assertIsNone(meso.read_amount(frame, box))

    def test_label_reads_the_area_right_of_the_box(self):
        frame = inventory_frame()
        with mock.patch('map_name.recognize_lines', return_value=[('메소', .9)]) as recognize:
            self.assertTrue(meso.has_label(frame, (300, 400, 150, 18)))
        crop = recognize.call_args.args[0]
        self.assertGreater(crop.width, 18 * 3)
        with mock.patch('map_name.recognize_lines', return_value=[('0', .9)]):
            self.assertFalse(meso.has_label(frame, (300, 400, 150, 18)))


class ConfirmationTests(unittest.TestCase):
    def test_value_needs_two_sightings_in_a_row(self):
        confirm = meso.Confirmation()
        self.assertIsNone(confirm.observe(100))
        self.assertIsNone(confirm.observe(None))
        self.assertIsNone(confirm.observe(100))
        self.assertEqual(confirm.observe(100), 100)
        # Already trusted values keep reporting, so a session sees the unchanged meso at its end.
        self.assertEqual(confirm.observe(100), 100)

    def test_tenfold_jump_needs_a_third_sighting(self):
        confirm = meso.Confirmation()
        confirm.observe(8890080); confirm.observe(8890080)
        self.assertIsNone(confirm.observe(889008))
        self.assertIsNone(confirm.observe(889008))
        self.assertEqual(confirm.observe(889008), 889008)


class SessionMesoTests(unittest.TestCase):
    def test_hunted_needs_two_readings(self):
        session = meso.SessionMeso()
        self.assertIsNone(session.hunted())
        session.observe(1000)
        self.assertIsNone(session.hunted())
        session.observe(1000)
        self.assertEqual(session.hunted(), 0)

    def test_large_changes_are_kept_out_of_hunting_meso(self):
        session = meso.SessionMeso()
        for value in (1_000_000, 1_050_000, 3_050_000, 3_100_000, 600_000):
            session.observe(value, threshold=1_000_000)
        self.assertEqual(session.trade, 2_000_000 - 2_500_000)
        self.assertEqual(session.hunted(), 100_000)

    def test_round_trip(self):
        session = meso.SessionMeso()
        session.observe(10); session.observe(20)
        again = meso.SessionMeso(session.as_dict())
        self.assertEqual((again.start, again.end, again.trade, again.hunted()), (10, 20, 0, 10))
        self.assertIsNone(meso.SessionMeso().as_dict())
        self.assertIsNone(meso.SessionMeso('broken').hunted())


class LogTests(unittest.TestCase):
    def test_log_and_day_change(self):
        with tempfile.TemporaryDirectory() as folder:
            meso.append(folder, '곰곰', 100, datetime(2026, 11, 22, 9))
            meso.append(folder, '다른캐', 5, datetime(2026, 11, 22, 10))
            meso.append(folder, '곰곰', 350, datetime(2026, 11, 22, 21))
            meso.append(folder, '곰곰', 900, datetime(2026, 11, 23, 1))
            with (Path(folder) / 'records' / 'meso.csv').open('a', encoding='utf-8') as stream:
                stream.write('broken,line\n')
            entries = meso.load(folder)
            self.assertEqual(len(entries), 4)
            self.assertEqual(meso.day_change(entries, '곰곰', date(2026, 11, 22)), 250)
            self.assertIsNone(meso.day_change(entries, '다른캐', date(2026, 11, 22)))
            self.assertEqual(meso.load(Path(folder) / 'missing'), [])


class RecordTests(unittest.TestCase):
    def test_saved_record_keeps_session_meso(self):
        import json
        import record_history
        import tracker
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(tracker, 'BASE', Path(base)):
                session = tracker.Session(level=170)
                for stamp, value in ((0, (219671043, 51.32)), (1, (219671043, 51.32)),
                                     (2, (219676043, 51.32)), (62, (219681043, 51.33))):
                    session.accept(value, stamp)
                for value in (1_000_000, 1_120_000):
                    session.meso.observe(value)
                writer = tracker.App.__new__(tracker.App)
                writer.session = session
                with mock.patch.object(tracker.time, 'monotonic', return_value=62):
                    writer.save_observation()
            saved = json.loads((Path(base)/'records'/f'{session.session_id}.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['meso'], {'start': 1_000_000, 'end': 1_120_000, 'trade': 0, 'readings': 2})
            row, = record_history.collect(base)
            self.assertEqual((row['meso'], row['meso_trade']), (120_000, 0))

    def test_trade_threshold_setting_is_validated(self):
        import json
        import tracker
        with tempfile.TemporaryDirectory() as base, mock.patch.object(tracker, 'log_error'):
            path = Path(base)/'settings.json'
            path.write_text(json.dumps({'meso_trade_threshold': 500000}), encoding='utf-8')
            self.assertEqual(tracker.load_settings(path)[0]['meso_trade_threshold'], 500000)
            for bad in (0, -5, '1000', 1.5):
                path.write_text(json.dumps({'meso_trade_threshold': bad}), encoding='utf-8')
                self.assertNotIn('meso_trade_threshold', tracker.load_settings(path)[0])

    def test_record_cells(self):
        import record_history
        self.assertEqual(record_history.meso_cell({'meso': None}), '—')
        self.assertEqual(record_history.meso_cell({'meso': 123456}), '+123,456')
        text = record_history.meso_detail({'meso': 60000, 'meso_trade': -500000, 'active': 600})
        self.assertEqual(text, '메소 +60,000 · 분당 6,000 (거래·구매 -500,000 제외)')
        self.assertIn('확인 안 됨', record_history.meso_detail({'meso': None}))


if __name__ == '__main__':
    unittest.main()

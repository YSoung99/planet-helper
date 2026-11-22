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


def add_amount_glyphs(frame, box, text='105'):
    x, y, w, _ = box
    ImageDraw.Draw(frame).text((x + w - 28, y + 4), text, fill=(0, 0, 0))
    return frame


class ReaderTests(unittest.TestCase):
    def test_confirmation_area_covers_only_verified_label_and_amount(self):
        box = (300, 400, 150, 18)
        frame = add_amount_glyphs(inventory_frame(), box)
        reader = meso.Reader()
        self.assertIsNone(reader.confirmation_area(frame.size))
        with mock.patch.object(meso, 'find_label', return_value=(260, 399, 300, 419)), \
             mock.patch.object(meso, 'read_amount', return_value=105), \
             mock.patch.object(meso, 'read_binary_amount', return_value=105):
            self.assertEqual(reader.read(frame), 105)
            self.assertEqual(reader.confirmation_area(frame.size), (260, 399, 450, 419))
            self.assertIsNone(reader.confirmation_area((810, 600)))
            self.assertIsNone(reader.read(inventory_frame()))
            self.assertIsNone(reader.confirmation_area(frame.size))

    def test_white_boxes_finds_wide_boxes_only(self):
        frame = inventory_frame()
        ImageDraw.Draw(frame).rectangle((500, 100, 540, 140), fill=(255, 255, 255))  # a square, not a box
        self.assertEqual(sorted(meso.white_boxes(frame)), [(60, 200, 120, 18), (300, 400, 150, 18)])

    def test_box_without_the_meso_label_is_ignored(self):
        frame = add_amount_glyphs(inventory_frame(), (300, 400, 150, 18))
        calls = []

        def label(frame, box):
            calls.append(box)
            return meso.label_context(frame, box) if box == (300, 400, 150, 18) else None
        with mock.patch.object(meso, 'find_label', side_effect=label), \
             mock.patch.object(meso, 'read_amount', return_value=8890080), \
             mock.patch.object(meso, 'read_binary_amount', return_value=8890080):
            reader = meso.Reader()
            self.assertEqual(reader.read(frame), 8890080)
        self.assertEqual(reader.box, (300, 400, 150, 18))

    def test_cached_box_rechecks_label_after_the_inventory_moves(self):
        original = (300, 400, 150, 18)
        moved = (24, 38, 150, 18)
        reader = meso.Reader()
        first = add_amount_glyphs(inventory_frame(original), original)
        second = add_amount_glyphs(inventory_frame(moved, trade_box=original), moved)
        ImageDraw.Draw(second).point((310, 405), fill=(0, 0, 0))
        with mock.patch.object(meso, 'read_amount', return_value=5) as read, \
             mock.patch.object(meso, 'read_binary_amount', return_value=5), \
             mock.patch.object(meso, 'find_label', side_effect=lambda frame, box:
                               meso.label_context(frame, box) if (frame is first and box == original)
                               or (frame is second and box == moved) else None):
            self.assertEqual(reader.read(first), 5)
            calls = read.call_count
            self.assertEqual(reader.read(first), 5)
            self.assertEqual(read.call_count, calls)
            self.assertEqual(reader.read(second), 5)
            self.assertEqual(reader.box, moved)
            self.assertIsNone(reader.read(Image.new('RGB', first.size)))
            self.assertIsNone(reader.box)

    def test_same_digits_survive_an_inventory_move_without_repeating_amount_ocr(self):
        original = (300, 400, 150, 18)
        moved = (24, 38, 150, 18)
        first = add_amount_glyphs(inventory_frame(original), original)
        second = add_amount_glyphs(inventory_frame(moved), moved)
        reader = meso.Reader()
        with mock.patch.object(meso, 'read_amount', return_value=105) as color, \
             mock.patch.object(meso, 'read_binary_amount', return_value=105) as binary, \
             mock.patch.object(meso, 'find_label', side_effect=meso.label_context) as label:
            self.assertEqual(reader.check(first, original, 1), 105)
            self.assertEqual(reader.check(second, moved, 2), 105)
        self.assertEqual(color.call_count, 1)
        self.assertEqual(binary.call_count, 1)
        self.assertEqual(label.call_count, 2)

    def test_label_patch_is_reused_only_while_its_pixels_remain_unchanged(self):
        box = (300, 400, 150, 18)
        label_rect = (450, 400, 478, 418)
        frame = add_amount_glyphs(inventory_frame(box), box)
        animated = frame.copy()
        animated.putpixel((285, 410), (255, 20, 40))
        obscured = animated.copy()
        obscured.putpixel((454, 409), (255, 255, 255))
        reader = meso.Reader()
        with mock.patch.object(meso, 'read_amount', return_value=105) as color, \
             mock.patch.object(meso, 'read_binary_amount', return_value=105) as binary, \
             mock.patch.object(meso, 'find_label', side_effect=[label_rect, None]) as label, \
             mock.patch.object(meso, 'white_boxes', return_value=[box]):
            self.assertEqual(reader.read(frame, 1), 105)
            self.assertEqual(reader.read(animated, 2), 105)
            self.assertEqual(label.call_count, 1)
            self.assertIsNone(reader.read(obscured, 3))
        self.assertEqual(label.call_count, 2)
        self.assertEqual(color.call_count, 1)
        self.assertEqual(binary.call_count, 1)

    def test_changed_digits_need_color_and_binary_ocr_to_agree(self):
        box = (300, 400, 150, 18)
        first = add_amount_glyphs(inventory_frame(box), box, '105')
        lower = add_amount_glyphs(inventory_frame(box), box, '95')
        reader = meso.Reader()
        with mock.patch.object(meso, 'read_amount', side_effect=[105, 95, 95]), \
             mock.patch.object(meso, 'read_binary_amount', side_effect=[105, 85, 95]), \
             mock.patch.object(meso, 'find_label', side_effect=meso.label_context):
            self.assertEqual(reader.check(first, box, 1), 105)
            self.assertIsNone(reader.read(lower, 2))
            self.assertTrue(reader.needs_recheck)
            self.assertEqual(reader.last_glyphs[1], 105)
            self.assertEqual(reader.read(lower, 3), 95)
            self.assertFalse(reader.needs_recheck)

    def test_opaque_strip_does_not_leave_a_readable_suffix(self):
        frame = Image.new('RGB', (600, 400), (40, 60, 90))
        box = (130, 250, 350, 52)
        x, y, w, h = box
        draw = ImageDraw.Draw(frame)
        draw.rectangle((x, y, x + w - 1, y + h - 1), fill='white')
        draw.rectangle((x + 270, y + 7, x + 279, y + 30), fill='black')
        self.assertIsNotNone(meso.amount_glyphs(frame, box))
        draw.rectangle((x + 185, y + 6, x + 237, y + 36), fill=(40, 50, 70))
        self.assertIsNone(meso.amount_glyphs(frame, box))

    def test_short_balance_uses_the_tight_crop_and_still_needs_agreement(self):
        box = (300, 400, 150, 18)
        frame = add_amount_glyphs(inventory_frame(box), box, '0')
        for binary, expected in ((0, 0), (8, None), (None, None)):
            with self.subTest(binary=binary), mock.patch.object(
                meso, 'read_amount', return_value=None
            ), mock.patch.object(meso, 'read_gray_amount', return_value=0) as gray, \
                 mock.patch.object(meso, 'read_binary_amount', return_value=binary), \
                 mock.patch.object(meso, 'find_label', side_effect=meso.label_context):
                reader = meso.Reader()
                self.assertEqual(reader.check(frame, box, 1), expected)
                gray.assert_called_once()
                self.assertEqual(reader.needs_recheck, expected is None)

    def test_binary_retries_native_scale_only_when_the_enlarged_read_fails(self):
        glyph = Image.new('L', (15, 22), 255)
        with mock.patch.object(meso, 'recognize_amount', side_effect=[None, 0]) as recognize:
            self.assertEqual(meso.read_binary_amount(glyph), 0)
        self.assertEqual([call.args[0].size for call in recognize.call_args_list],
                         [(46, 60), (35, 32)])

    def test_amount_must_be_a_clean_number(self):
        frame = inventory_frame()
        box = (300, 400, 150, 18)
        for text, expected in (('8,890,080', 8890080), ('0', 0), ('8.890.080', 8890080),
                               ('8,89O,080', None), ('8,890,0', None), ('', None)):
            with mock.patch('map_name.recognize_lines', return_value=[(text, .99)]):
                self.assertEqual(meso.read_amount(frame, box), expected, text)
        with mock.patch('map_name.recognize_lines', return_value=[('8,890,080', .5)]):
            self.assertIsNone(meso.read_amount(frame, box))

    def test_new_ui_button_edge_is_dropped_from_the_amount(self):
        frame = inventory_frame()
        with mock.patch('map_name.recognize_lines', return_value=[('메24,252,276', .91)]):
            self.assertEqual(meso.read_amount(frame, (300, 400, 150, 18)), 24252276)

    def test_label_is_looked_for_on_both_sides_of_the_box(self):
        frame = inventory_frame()
        box = (300, 400, 150, 18)
        with mock.patch('map_name.recognize_lines', return_value=[('메소', .9), ('24,252,276', .99)]) as recognize:
            self.assertTrue(meso.has_label(frame, box))
        crop = recognize.call_args.args[0]
        self.assertEqual(recognize.call_args.kwargs, {'detect': True})
        # 2.6 box heights on each side, scaled up, plus padding.
        self.assertGreater(crop.width, (150 + 2 * 18 * 2) * 2)
        for lines in ([('8,890,080 메소', .95)], [('0', .9)]):
            with mock.patch('map_name.recognize_lines', return_value=lines):
                self.assertEqual(meso.has_label(frame, box), lines[0][0] != '0')

    def test_small_new_ui_reads_the_label_to_the_left_without_text_detection(self):
        frame = inventory_frame()
        box = (180, 496, 185, 23)
        readings = [[('24,608,229', .99)], [('메소', .98382)]]
        with mock.patch('map_name.recognize_lines', side_effect=readings) as recognize:
            self.assertTrue(meso.has_label(frame, box))
        label = recognize.call_args.args[0]
        self.assertEqual(recognize.call_args.kwargs, {'detect': False})
        self.assertEqual(label.size, (2 * round(23 * 2.45) + 24, 2 * 23 + 24))

    def test_native_size_new_ui_retries_the_left_label_without_accepting_other_text(self):
        frame = inventory_frame()
        box = (100, 200, 361, 53)
        for reading, expected in ((('메소', .93), True), (('메소', .6), False),
                                  (('메소 상점', .98), False)):
            with self.subTest(reading=reading), mock.patch(
                'map_name.recognize_lines', side_effect=[[('25,936,640', .99)], [reading]]
            ) as recognize:
                self.assertEqual(meso.has_label(frame, box), expected)
                label = recognize.call_args.args[0]
                self.assertEqual(label.size, (2 * round(53 * 1.55) + 24, 2 * 53 + 24))

    def test_box_merged_with_bright_ui_above_is_split_at_its_border(self):
        frame = Image.new('RGB', (800, 600), (40, 60, 90))
        draw = ImageDraw.Draw(frame)
        draw.rectangle((300, 380, 520, 399), fill=(250, 250, 250))   # bright panel above
        draw.line((300, 400, 520, 400), fill=(150, 150, 150))        # box border
        draw.rectangle((300, 401, 520, 440), fill=(255, 255, 255))   # the meso box
        draw.rectangle((300, 395, 303, 405), fill=(255, 255, 255))   # a gap joins them
        for x in range(380, 500, 12):                                 # digits darken columns
            draw.rectangle((x, 404, x + 5, 437), fill=(20, 20, 20))
        boxes = meso.white_boxes(frame)
        self.assertIn((300, 401, 221, 40), boxes)

    def test_longest_run_bridges_short_gaps(self):
        flags = [True] * 5 + [False] * 2 + [True] * 5 + [False] * 9 + [True] * 7
        self.assertEqual(meso.longest_run(flags), (21, 28))
        self.assertEqual(meso.longest_run(flags, bridge=2), (0, 12))
        self.assertEqual(meso.longest_run([]), (0, 0))

    def test_numbers_without_the_label_are_skipped_for_a_while(self):
        frame = inventory_frame()
        add_amount_glyphs(frame, (60, 200, 120, 18))
        add_amount_glyphs(frame, (300, 400, 150, 18))
        reader = meso.Reader()
        with mock.patch.object(meso, 'read_amount', return_value=0), \
             mock.patch.object(meso, 'find_label', return_value=None) as label:
            self.assertIsNone(reader.read(frame, now=0))
            self.assertIsNone(reader.read(frame, now=10))
            self.assertEqual(label.call_count, 2)  # two boxes, checked once
            self.assertIsNone(reader.read(frame, now=31))
            self.assertEqual(label.call_count, 4)


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
    def test_duplicate_and_distant_frames_do_not_confirm(self):
        confirm = meso.Confirmation()
        self.assertIsNone(confirm.observe(100, 1))
        self.assertIsNone(confirm.observe(100, 1))
        self.assertIsNone(confirm.observe(100, 20))
        self.assertEqual(confirm.observe(100, 22), 100)

    def test_single_opening_and_stale_end_do_not_complete(self):
        session = meso.SessionMeso()
        source = (1, '본캐', 10)
        for stamp in (2, 4, 6):
            session.sample(100, source, stamp, 1, 0, 100)
        session.finish(source, 7)
        self.assertIsNone(session.hunted())
        session.sample(100, source, 30, 2, 0, 100)
        session.finish(source, 45)
        self.assertIsNone(session.hunted())
        session.finish(source, 32)
        self.assertEqual(session.hunted(), 0)

    def test_switching_windows_invalidates_the_delta_even_after_returning(self):
        session = meso.SessionMeso()
        source = (1, '본캐', 10)
        self.assertTrue(session.sample(100, source, 2, 1, 0, 100))
        self.assertFalse(session.sample(900, (1, '본캐', 20), 20, 2, 0, 100))
        self.assertFalse(session.sample(300, source, 30, 3, 0, 100))
        session.finish(source, 31)
        self.assertIsNone(session.hunted())

    def test_malformed_saved_balance_is_ignored(self):
        session = meso.SessionMeso({'start': '100', 'end': 200, 'readings': '2',
                                    'large_changes': 1, 'marked': [[0]]})
        self.assertIsNone(session.hunted())
        self.assertIsNone(session.as_dict())

    def test_large_changes_count_until_marked_as_trade(self):
        session = meso.SessionMeso()
        source = (1, '본캐', 10)
        session.sample(1_000_000, source, 2, 1, 0, 1_000_000)
        session.sample(3_050_000, source, 32, 2, 0, 1_000_000)
        session.finish(source, 34)
        self.assertEqual(session.hunted(), 2_050_000)
        session.toggle_trade(0)
        self.assertEqual(session.hunted(), 0)
        self.assertEqual(meso.SessionMeso(session.as_dict()).hunted(), 0)

    def test_round_trip(self):
        session = meso.SessionMeso()
        session.observe(10); session.observe(20)
        session.complete = True
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
    def test_daily_log_loads_once_per_day_and_keeps_character_totals(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.object(meso, 'iter_entries', wraps=meso.iter_entries) as load:
            log = meso.DailyLog()
            first = datetime(2026, 11, 22, 10)
            log.record(base, '본캐', 10, first)
            log.record(base, '본캐', 30, first)
            log.record(base, '부캐', 90, first)
            self.assertEqual(log.change('본캐'), 20)
            self.assertEqual(log.change('부캐'), 0)
            self.assertEqual(load.call_count, 1)
            log.record(base, '본캐', 30, datetime(2026, 11, 23, 10))
            self.assertEqual(log.change('본캐'), 0)
            self.assertEqual(load.call_count, 2)

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
                session.meso.complete = True
                writer = tracker.App.__new__(tracker.App)
                writer.session = session
                with mock.patch.object(tracker.time, 'monotonic', return_value=62):
                    writer.save_observation()
            saved = json.loads((Path(base)/'records'/f'{session.session_id}.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['meso'], {'start': 1_000_000, 'end': 1_120_000, 'trade': 0, 'readings': 2,
                                             'complete': True, 'large_changes': [], 'marked': []})
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

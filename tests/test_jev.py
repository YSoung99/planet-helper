import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import exp_buffs
import jev
import jev_assist
import meso
import tracker


def sync_arbiter(enabled=True, cooldown=0.0):
    return jev.Arbiter(lambda: enabled, cooldown=cooldown, runner=lambda work: work())


class ArbiterTests(unittest.TestCase):
    def test_answers_are_kept_by_key_and_asked_once(self):
        arbiter = sync_arbiter()
        calls = []
        self.assertEqual(arbiter.answer('a'), (False, None))
        self.assertTrue(arbiter.submit('a', lambda: calls.append(1) or 'yes'))
        self.assertFalse(arbiter.submit('a', lambda: calls.append(2) or 'no'))
        self.assertEqual(arbiter.answer('a'), (True, 'yes'))
        self.assertEqual(calls, [1])

    def test_disabled_busy_and_cooldown_refuse_new_questions(self):
        self.assertFalse(sync_arbiter(enabled=False).submit('a', lambda: 1))
        clock = [100.0]
        started = []
        arbiter = jev.Arbiter(cooldown=10, clock=lambda: clock[0], runner=started.append)
        self.assertTrue(arbiter.submit('a', lambda: 1))
        self.assertFalse(arbiter.submit('b', lambda: 2))   # one question at a time
        started.pop()()
        self.assertFalse(arbiter.submit('b', lambda: 2))   # cooldown
        clock[0] += 10
        self.assertTrue(arbiter.submit('b', lambda: 2))

    def test_unavailable_question_is_asked_again_after_the_retry_wait(self):
        clock = [0.0]
        arbiter = jev.Arbiter(cooldown=10, clock=lambda: clock[0], runner=lambda work: work())

        def no_key():
            raise jev.Unavailable('JEV API 키 없음')
        self.assertTrue(arbiter.submit('a', no_key))
        self.assertEqual(arbiter.answer('a'), (False, None))
        clock[0] = jev.RETRY_SECONDS - 1
        self.assertFalse(arbiter.submit('a', lambda: 'yes'))
        clock[0] = jev.RETRY_SECONDS
        self.assertTrue(arbiter.submit('a', lambda: 'yes'))
        self.assertEqual(arbiter.answer('a'), (True, 'yes'))

    def test_a_failing_question_is_answered_with_none(self):
        arbiter = sync_arbiter()
        with patch('tracker.log_error'):
            arbiter.submit('a', lambda: 1/0)
        self.assertEqual(arbiter.answer('a'), (True, None))
        self.assertTrue(arbiter.submit('b', lambda: 2))


class AskTests(unittest.TestCase):
    def reply(self, value, probability):
        return {'answers': [{'name': 'answer', 'probabilities': [
            {'value': value, 'probability': probability}, {'value': 'none', 'probability': 1-probability}]}]}

    def test_only_a_listed_confident_answer_is_returned(self):
        picture = Image.new('RGB', (40, 10))
        with patch('map_name.gateway_key', return_value='k'), patch('jev.request') as request:
            request.return_value = self.reply('1', .95)
            self.assertEqual(jev.ask([('Crop', picture)], 'q', [('0', 'a'), ('1', 'b')]), '1')
            content, questions, key = request.call_args.args
            self.assertEqual(key, 'k')
            self.assertEqual([c['value'] for c in questions[0]['choices']], ['0', '1', 'none'])
            self.assertEqual(sum(part['type'] == 'input_image' for part in content), 1)
            request.return_value = self.reply('1', .8)
            self.assertIsNone(jev.ask([('Crop', picture)], 'q', [('0', 'a'), ('1', 'b')]))
            request.return_value = self.reply('7', .99)
            self.assertIsNone(jev.ask([('Crop', picture)], 'q', [('0', 'a'), ('1', 'b')]))

    def test_no_key_or_network_failure_is_unavailable_not_unsure(self):
        picture = Image.new('RGB', (40, 10))
        with patch('map_name.gateway_key', return_value=None), patch('jev.request') as request:
            with self.assertRaises(jev.Unavailable):
                jev.ask([('Crop', picture)], 'q', [('0', 'a')])
            request.assert_not_called()
        with patch('map_name.gateway_key', return_value='k'), \
                patch('jev.request', side_effect=OSError('offline')), patch('tracker.log_error'):
            with self.assertRaises(jev.Unavailable):
                jev.ask([('Crop', picture)], 'q', [('0', 'a')])

    def test_image_is_sent_as_a_square(self):
        url = jev.image_url(Image.new('RGB', (300, 20)))
        import base64, io
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
            self.assertEqual(image.size, (jev.SIDE, jev.SIDE))


class ReadingTieBreakTests(unittest.TestCase):
    def test_meso_disagreement_waits_for_jev_and_then_uses_its_pick(self):
        arbiter = sync_arbiter()
        reader = meso.Reader(arbiter)
        glyphs = Image.new('L', (30, 10))
        key = glyphs.size, glyphs.tobytes()
        with patch('jev.pick_reading', return_value=None):
            self.assertIsNone(reader.judge(glyphs, key, 1_234_567, 1_234_561))
        self.assertIsNone(reader.judge(glyphs, key, 1_234_567, 1_234_561))  # JEV unsure: keep rechecking
        other = Image.new('L', (30, 11))
        with patch('jev.pick_reading', return_value=1) as pick:
            self.assertIsNone(reader.judge(other, (other.size, other.tobytes()), 1_234_567, 1_234_561))
            self.assertEqual(pick.call_args.args[1], ['1,234,567', '1,234,561'])
        self.assertEqual(reader.judge(other, (other.size, other.tobytes()), 1_234_567, 1_234_561), 1_234_561)
        self.assertIsNone(meso.Reader().judge(glyphs, key, 1, 2))
        self.assertIsNone(reader.judge(glyphs, key, 1, None))

    def test_exp_disagreement_between_two_valid_readings_uses_jev(self):
        reader = tracker.LineReader(engine=None)
        image = Image.new('RGB', (60, 12))
        key = (image.mode, image.size, image.tobytes(), 200)
        readings = [((341_117_616, 50.0), .95, 'a'), ((341_117_615, 50.0), .93, 'b'), (None, .4, 'c')]
        self.assertIsNone(reader.judged(image, key, readings))   # no arbiter: unchanged behaviour
        reader.arbiter = sync_arbiter()
        with patch('jev.pick_reading', return_value=1) as pick:
            self.assertIsNone(reader.judged(image, key, readings))
            self.assertEqual(pick.call_args.args[1], ['341,117,616 [50.00%]', '341,117,615 [50.00%]'])
        self.assertEqual(reader.judged(image, key, readings), ((341_117_615, 50.0), .93, 'b'))
        # One candidate, or three, is not a tie JEV should break.
        self.assertIsNone(reader.judged(image, key, readings[:1]))
        self.assertIsNone(reader.judged(image, key, readings[:2] + [((341_117_617, 50.0), .9, 'd')]))


class InventoryHintTests(unittest.TestCase):
    def test_small_box_is_found_only_inside_the_enlarged_hint(self):
        from PIL import ImageDraw
        frame = Image.new('RGB', (800, 600), (40, 60, 90))
        ImageDraw.Draw(frame).rectangle((600, 500, 659, 505), fill=(250, 250, 250))   # 6px tall: too small
        reader = meso.Reader()
        with patch.object(meso, 'has_label', return_value=True), \
                patch.object(meso, 'amount_glyphs', return_value=Image.new('L', (20, 10))), \
                patch.object(meso, 'read_amount', return_value=4_200), \
                patch.object(meso, 'read_binary_amount', return_value=4_200):
            self.assertIsNone(reader.read(frame))
            self.assertFalse(reader.located)
            reader.hint = jev_assist.quadrant_region('br')
            self.assertEqual(reader.read(frame), 4_200)
            self.assertTrue(reader.located)

    def test_quarter_hint_needs_sixty_percent_and_closed_needs_ninety(self):
        frame = Image.new('RGB', (900, 600))

        def reply(value, probability):
            return {'answers': [{'name': 'answer', 'probabilities': [
                {'value': value, 'probability': probability},
                {'value': 'none', 'probability': 1-probability}]}]}
        with patch('map_name.gateway_key', return_value='k'), patch('jev.request') as request:
            for value, probability, expected in [('br', .79, 'br'), ('tr', .41, None),
                                                 ('closed', .49, None), ('closed', .95, 'closed')]:
                with self.subTest(value=value, probability=probability):
                    request.return_value = reply(value, probability)
                    self.assertEqual(jev_assist.ask_inventory(frame), expected)
        self.assertEqual(jev_assist.quadrant_region('br'), (.4, .4, 1.0, 1.0))


class BuffConfirmationTests(unittest.TestCase):
    def test_inactive_reading_is_shown_as_absent(self):
        confirmation = exp_buffs.Confirmation()
        confirmation.observe((1, 2), 10.0, {'coupon': 'inactive', 'holy_symbol': 'unknown'})
        self.assertEqual(confirmation.text(1, 11.0), '경쿠 없음 · 심볼 확인 불가')


class AssistTests(unittest.TestCase):
    def test_stall_reasons_are_saved_per_record(self):
        with tempfile.TemporaryDirectory() as folder:
            jev_assist.save_stall('s1', 3, '사망', folder)
            jev_assist.save_stall('s1', 9, '마을·상점', folder)
            jev_assist.save_stall('s1', 12, '사망', folder)
            stalls = jev_assist.load_stalls(folder)
        self.assertEqual(jev_assist.stall_text(stalls['s1']), '사망 2회 · 마을·상점 1회')

    def test_choices_map_back_to_korean_text(self):
        frame = Image.new('RGB', (100, 60))
        with patch('jev.ask', return_value='0') as ask:
            self.assertEqual(jev_assist.ask_stall(frame), '사망')
            self.assertEqual(ask.call_args.args[2][0], ('0', jev_assist.STALL_CHOICES[0][0]))
        with patch('jev.ask', return_value=None):
            self.assertIsNone(jev_assist.ask_change(frame, frame))
        with patch('jev.ask', return_value='1'):
            self.assertEqual(jev_assist.ask_buff(frame, frame), 'inactive')
            self.assertEqual(jev_assist.ask_nickname(frame, ['본캐', '부캐', '본캐']), '부캐')
        self.assertIsNone(jev_assist.ask_nickname(frame, []))

    def test_frame_ring_keeps_reduced_frames_at_most_every_period(self):
        ring = jev_assist.FrameRing()
        ring.remember(0, Image.new('RGB', (1920, 1080)))
        ring.remember(5, Image.new('RGB', (1920, 1080), 'white'))
        ring.remember(25, Image.new('RGB', (800, 600)))
        self.assertEqual(len(ring.frames), 2)
        self.assertEqual(ring.before(10).size, (960, 540))
        self.assertEqual(ring.latest().size, (800, 600))
        self.assertIsNone(ring.latest(since=26))
        self.assertIsNone(ring.before(-1))


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from mac_app import Controller
        self.temp = tempfile.TemporaryDirectory()
        self.base = patch.object(tracker, 'BASE', Path(self.temp.name))
        self.base.start()
        self.c = Controller(self.app, start_worker=False, restore=False)
        self.c.timer.stop()
        self.c.jev_arbiter = jev.Arbiter(lambda: self.c.jev_enabled, cooldown=0, runner=lambda work: work())

    def tearDown(self):
        self.c.key_repeat.shutdown(); self.c.start_captures.close(); self.c.window.hide(); self.c.window.deleteLater()
        self.app.processEvents(); self.base.stop(); self.temp.cleanup()

    def test_nothing_is_asked_while_jev_is_off(self):
        c = self.c
        c.running = True
        c.session.started = 0.0
        c.hunt_activity.update(c.generation, 0.0, gained=True)
        c.jev_frames.remember(40.0, Image.new('RGB', (100, 60)))
        with patch('jev_assist.ask_stall') as ask:
            c.apply_jev_hints(100.0)
            ask.assert_not_called()

    def test_stall_reason_is_asked_once_shown_and_saved(self):
        c = self.c
        c.jev_enabled = True
        c.running = True
        c.session.started = 0.0
        c.hunt_activity.update(c.generation, 0.0, gained=True)
        c.jev_frames.remember(40.0, Image.new('RGB', (100, 60)))
        with patch('jev_assist.ask_stall', return_value='사망') as ask:
            c.apply_jev_hints(30.0)       # not a minute yet
            ask.assert_not_called()
            c.apply_jev_hints(70.0)
            c.activity_label.setText('획득 대기 · 사냥 여부 확인 중')
            c.apply_jev_hints(71.0)
            c.apply_jev_hints(72.0)
        ask.assert_called_once()
        self.assertEqual(c.activity_label.text(), '획득 대기 · 사냥 여부 확인 중 · JEV: 사망')
        self.assertEqual(jev_assist.load_stalls()[c.session.session_id], {'0': '사망'})

    def test_screen_hint_shows_until_the_hud_is_found(self):
        c = self.c
        c.jev_enabled = True
        c.running = True
        frame = Image.new('RGB', (100, 60))
        with patch('qt_controller.time.monotonic', side_effect=[100.0, 120.0]), \
                patch('jev_assist.ask_screen', return_value='로그인·캐릭터 선택 화면이에요'):
            c.note_missing_hud(c.generation, frame)
            c.note_missing_hud(c.generation, frame)
        c.apply_jev_hints(120.0)
        self.assertEqual(c.jev_status.text(), 'JEV 화면 확인 · 로그인·캐릭터 선택 화면이에요')
        c.session.first = (1, 1.0)
        c.apply_jev_hints(121.0)
        self.assertTrue(c.jev_status.isHidden())

    def test_change_prompt_gets_a_suggested_tag(self):
        c = self.c
        c.jev_enabled = True
        c.running = True
        c.session.started = 0.0
        c.jev_frames.remember(200.0, Image.new('RGB', (100, 60)))
        c.jev_frames.remember(700.0, Image.new('RGB', (100, 60), 'white'))
        change = dict(minute=6, before=1000.0, after=500.0)
        c.pending_change = change
        c.change_prompt_text = '▼ 6분째부터 · 뭐가 달라졌나요? (눌러서 태그)'
        with patch('jev_assist.ask_change', return_value='경험치 버프 변화') as ask:
            c.apply_jev_hints(701.0)
            c.apply_jev_hints(702.0)
        ask.assert_called_once()
        self.assertTrue(c.change_prompt.text().endswith('· JEV 추천: 경험치 버프 변화'))
        with patch('PySide6.QtWidgets.QInputDialog.getText', return_value=('', False)) as dialog:
            c.answer_change_prompt()
        self.assertEqual(dialog.call_args.kwargs['text'], '경험치 버프 변화')

    def test_unknown_buff_reading_uses_jev_answer_for_that_period(self):
        c = self.c
        c.jev_enabled = True
        exp_buffs.template_path(tracker.BASE, 'coupon').parent.mkdir(parents=True)
        Image.new('RGB', (30, 30), 'red').save(exp_buffs.template_path(tracker.BASE, 'coupon'))
        frame = Image.new('RGB', (400, 300))
        readings = {'coupon': 'unknown', 'holy_symbol': 'unregistered'}
        with patch('jev_assist.ask_buff', return_value='active') as ask:
            self.assertEqual(c.jev_buff_readings(frame, 10.0, readings), readings)
            self.assertEqual(c.jev_buff_readings(frame, 12.0, readings)['coupon'], 'active')
        ask.assert_called_once()
        self.assertEqual(readings['coupon'], 'unknown')

    def test_missing_meso_box_asks_where_the_inventory_is_then_waits_when_closed(self):
        c = self.c
        c.jev_enabled = True
        c.running = True
        frame = Image.new('RGB', (100, 60))
        source = (c.generation, 7)
        with patch('jev_assist.ask_inventory', side_effect=['closed', 'br']) as ask:
            c.locate_inventory(source, 0.0, frame)
            c.locate_inventory(source, 3.0, frame)
            ask.assert_not_called()
            c.locate_inventory(source, 7.0, frame)          # missing for 6 seconds: ask
            c.locate_inventory(source, 9.0, frame)
            self.assertTrue(c.meso_inventory_closed)
            self.assertEqual(c.meso_text(), '메소 · JEV: 인벤토리가 닫혀 있어요 · 열면 기록')
            c.locate_inventory(source, 500.0, frame)        # closed: wait ten minutes
            self.assertEqual(ask.call_count, 1)
            c.locate_inventory(source, 608.0, frame)
            c.locate_inventory(source, 610.0, frame)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(c.meso_reader.hint, jev_assist.quadrant_region('br'))
        self.assertFalse(c.meso_inventory_closed)

    def test_unsure_inventory_answer_also_waits_ten_minutes(self):
        c = self.c
        c.jev_enabled = True
        frame = Image.new('RGB', (100, 60))
        source = (c.generation, 7)
        with patch('jev_assist.ask_inventory', return_value=None) as ask:
            for stamp in (0.0, 7.0, 9.0, 70.0, 300.0, 606.0):
                c.locate_inventory(source, stamp, frame)
            self.assertEqual(ask.call_count, 1)
            c.locate_inventory(source, 607.0, frame)
        self.assertEqual(ask.call_count, 2)

    def test_unread_nickname_is_matched_to_a_known_name(self):
        c = self.c
        c.jev_enabled = True
        c.current_character = '본캐'
        c.nickname_patterns = {'old': '부캐'}
        frame = Image.new('RGB', (400, 300))
        with patch('character_hud.lines', return_value=(frame, frame)), \
                patch('jev_assist.ask_nickname', return_value='부캐') as ask:
            self.assertIsNone(c.jev_nickname(frame, (0, 0, .1, .1), 'classic', 'k1'))
            self.assertEqual(c.jev_nickname(frame, (0, 0, .1, .1), 'classic', 'k1'), '부캐')
        self.assertEqual(ask.call_args.args[1][:2], ['본캐', '부캐'])


if __name__ == '__main__':
    unittest.main()

import csv
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch
import tracker


def establish(session, value, stamp):
    """Provide two real-time observations before testing accounting behavior."""
    session.accept(value, stamp-1)
    return session.accept(value, stamp)


class AccountingTests(unittest.TestCase):
    def test_both_hud_formats_keep_the_same_experience_value(self):
        for text in ('34514347[8.06%]', 'EXP 34514347 [8.06%]', 'exp 34,514,347 [8.06%]', 'EXP. 34514347 [8.06%]'):
            with self.subTest(text=text):
                self.assertEqual(tracker.parse(text), (34514347,8.06))
        for text in ('HP34514347[8.06%]', 'BONUS EXP 34514347[8.06%]',
                     'EXP EXP34514347[8.06%]', 'EXP34514347 8.06%',
                     'EXP..34514347[8.06%]', 'EXP34514347[8.06%]123', 'EXP34514347[100.00%]'):
            with self.subTest(text=text):
                self.assertIsNone(tracker.parse(text))

    def test_waiting_does_not_start_hunt_and_first_gain_is_counted(self):
        s = tracker.Session()
        establish(s, (1000, 10), 10)
        s.accept((1000, 10), 100)
        self.assertIsNone(s.started)
        self.assertEqual(s.elapsed(100), 0)
        s.accept((1001, 10.01), 103)
        self.assertEqual(s.started, 103)
        self.assertEqual(s.stats(103)[0], 1)
        self.assertEqual(s.stats(103)[1], 0)
        self.assertEqual(s.elapsed(108), 5)
        self.assertEqual(s.stats(108)[1], 720)

    def test_zero_percent_baseline(self):
        self.assertEqual(tracker.parse('0[0.00%]'), (0, 0))
        s = tracker.Session()
        establish(s, (0, 0), 0)
        s.accept((10, 0.1), 2)
        self.assertEqual(s.started, 2)
        self.assertEqual(s.stats(8)[0], 10)

    def test_bad_percent_and_out_of_order_do_not_mutate_baseline(self):
        s = tracker.Session()
        establish(s, (1000, 10), 10)
        s.accept((1001, 9), 11)
        s.accept((999, 9.99), 9)
        self.assertIsNone(s.started)
        self.assertEqual(s.last, (1000, 10))
        self.assertFalse(s.blocked)
        s.accept((1001, 10.01), 12)
        self.assertEqual(s.started, 12)

    def test_stop_freezes_time_and_rates(self):
        s = tracker.Session()
        establish(s, (1000, 10), 0)
        s.accept((1100, 11), 2)
        s.ended = 12
        self.assertEqual(s.elapsed(900), 10)
        self.assertEqual(s.stats(20), s.stats(900))

    def test_recent_windows_interpolate_boundary(self):
        s = tracker.Session()
        s.samples = deque([(0, 0), (58, 580), (62, 620), (120, 1200)])
        self.assertEqual(s.recent_gained(60, 120), 600)
        s.samples = deque([(0, 0), (358, 3580), (362, 3620), (720, 7200)])
        self.assertEqual(s.recent_gained(360, 720), 3600)

    def test_no_extrapolation_and_uncertain_gaps_are_hidden(self):
        s = tracker.Session()
        s.samples = deque([(0, 0), (50, 500), (80, 800), (120, 1200)])
        self.assertIsNone(s.recent_gained(60, 120))
        self.assertIsNone(s.recent_gained(60, 127))
        s.samples = deque([(5, 100), (10, 160)])
        self.assertEqual(s.recent_gained(60, 14), 60)

    def test_automatic_ledger_preserves_baseline_first_gain_and_phases(self):
        s = tracker.Session()
        app = tracker.App.__new__(tracker.App)
        app.session = s
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            establish(s, (1000, 10), 0)
            app.save_observation()
            s.accept((1010, 10.1), 2)
            app.save_observation()
            with next((Path(folder)/'records').glob('*.csv')).open(encoding='utf-8-sig') as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1][-1], 'waiting')
            self.assertEqual(rows[2][-2:], ['10', 'hunting'])
            state = json.loads(next((Path(folder)/'records').glob('*.json')).read_text())
            self.assertEqual(state['baseline'], [1000, 10])
            self.assertEqual(state['gained'], 10)

    def test_level_169_table_matches_user_tooltip(self):
        self.assertEqual(tracker.EXP_TABLE[169], 405810702)
        self.assertEqual(tracker.EXP_METADATA['screenshot_verified']['169'], 405810702)
        self.assertNotIn('170', tracker.EXP_METADATA['screenshot_verified'])

    def test_exact_level_table_validates_initial_ocr_and_counts_first_gain(self):
        s = tracker.Session(level=169)
        establish(s, (172680132, 42.55), 0)
        self.assertEqual(s.total, 405810702)
        s.accept((172680133, 42.55), 2)
        self.assertEqual(s.started, 2)
        self.assertEqual(s.stats(10)[0], 1)
        self.assertEqual(s.total-s.last[0], 233130569)

    def test_wrong_level_percentage_cannot_become_baseline(self):
        s = tracker.Session(level=169)
        establish(s, (172680132, 43.30), 0)
        self.assertIsNone(s.first)
        self.assertEqual(s.total, 405810702)

    def test_minute_values_update_once_per_completed_minute_and_include_first_gain(self):
        s = tracker.Session()
        establish(s, (1000,10),0)
        s.accept((1010,10.1),2)
        self.assertIsNone(s.minute_result())
        for t in range(4,63,2):
            exp=1000+t*5
            s.accept((exp,exp/100),t)
        first=s.minute_result().copy()
        self.assertEqual(first['index'],1)
        self.assertEqual(first['one'],310)
        s.accept((1400,14),80)
        self.assertEqual(s.minute_result(),first)
        s.accept((1610,16.1),122)
        self.assertEqual(s.minute_result()['index'],2)
        self.assertEqual(s.minute_result()['one'],300)
        self.assertEqual(s.minute_result()['six'],610)

    def test_six_minute_snapshot_slides_over_completed_minutes(self):
        s=tracker.Session();establish(s, (1000,10),0);s.accept((1001,10.01),1)
        for t in range(2,482):
            exp=1000+t
            s.accept((exp,exp/100),t)
        result=s.minute_result()
        self.assertEqual(result['index'],8)
        self.assertEqual(result['one'],60)
        self.assertEqual(result['six'],360)

    def test_invalid_percent_is_held_without_stopping_or_counting_a_decrease(self):
        s=tracker.Session(level=169);establish(s, (245596661,60.52),0)
        for t in (1,2,3,4):s.accept((145596661,60.52),t)
        self.assertFalse(s.blocked)
        self.assertEqual(s.decrease_count,0)
        self.assertEqual(s.last[0],245596661)
        self.assertEqual(s.mismatch_count,4)
        recovered=245596761
        s.accept((recovered,round(recovered/s.total*100,2)),5)
        self.assertEqual(s.last[0],recovered)
        self.assertEqual(s.mismatch_count,0)

    def test_large_jump_waits_for_next_frame_and_keeps_first_gain_time(self):
        s=tracker.Session(level=169);establish(s, (172680132,42.55),0)
        big=182680132;pct=round(big/405810702*100,2)
        s.accept((big,pct),2)
        self.assertEqual(s.last[0],172680132)
        self.assertIsNone(s.started)
        s.accept((big,pct),3)
        self.assertEqual(s.last[0],big)
        self.assertEqual(s.started,2)
        self.assertEqual(s.stats(10)[0],10000000)

    def test_false_large_jump_does_not_poison_total(self):
        s=tracker.Session(level=169);establish(s, (172680132,42.55),0)
        big=182680132;s.accept((big,round(big/405810702*100,2)),2)
        s.accept((172680142,42.55),3)
        self.assertEqual(s.stats(10)[0],10)
        self.assertIsNone(s.pending_jump)

    def test_saved_record_restores_frozen_total_and_minute_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'session.csv'
            with path.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream)
                writer.writerow(['관측 시각','현재 경험치','퍼센트','사냥 초','총 획득 경험치','상태'])
                writer.writerows([
                    ['2026-10-30 10:00:00.000',172680132,42.55,0,0,'waiting'],
                    ['2026-10-30 10:00:02.000',172680142,42.55,0,10,'hunting'],
                    ['2026-10-30 10:01:02.000',172680152,42.55,60,20,'hunting']])
            restored=tracker.load_saved_session(path,169,1000)
            self.assertEqual(restored.stats(2000)[0],20)
            self.assertEqual(restored.elapsed(2000),60)
            self.assertEqual(restored.minute_result()['one'],20)
            self.assertEqual(restored.last,(172680152,42.55))

    def test_minute_snapshot_is_retained_during_missing_readings(self):
        s=tracker.Session();establish(s, (1000,10),0);s.accept((1010,10.1),2)
        s.accept((1310,13.1),62)
        snapshot=s.minute_result().copy()
        s.stats(5000)
        self.assertEqual(s.minute_result(),snapshot)

    def test_region_buffer_decodes_rgbx_and_closes_pipe_descriptors(self):
        import importlib.util
        if importlib.util.find_spec('dbus_fast') is None: self.skipTest('Linux optional capture dependency')
        import asyncio,os
        from types import SimpleNamespace
        from dbus_fast import MessageType,Variant
        class Bus:
            async def call(self,message):
                os.write(message.unix_fds[0],bytes([255,0,0,255,0,255,0,255]))
                meta={key:Variant(signature,value) for key,signature,value in [
                    ('type','s','raw'),('format','u',16),('width','u',2),('height','u',1),('stride','u',8),('scale','d',1.)]}
                return SimpleNamespace(message_type=MessageType.METHOD_RETURN,body=[meta])
        capture=tracker.KWinRegionCapture.__new__(tracker.KWinRegionCapture)
        capture.loop=asyncio.new_event_loop();capture.bus=Bus()
        try:
            image=capture.grab({'left':0,'top':0,'width':2,'height':1})
            self.assertEqual([image.getpixel((0,0)),image.getpixel((1,0))],[(255,0,0),(0,255,0)])
        finally:capture.loop.close()

    def test_load_targets_stay_within_one_to_five_seconds(self):
        c = tracker.LoadController()
        self.assertEqual(c.period(10, 0.1), 1)
        self.assertGreater(c.period(90, 0.1), 1)
        self.assertEqual(c.period(10, 4), 5)
        c.base = 25
        self.assertEqual(c.period(70, 0), 5)
        for load in (0, 49, 50, 65, 84, 85, 95, 100):
            for cost in (0, .1, 1, 5, 100):
                period = c.period(load, cost)
                self.assertGreaterEqual(period, 1)
                self.assertLessEqual(period, 5)

    def test_unchanged_pixels_skip_ocr_but_changes_and_expiry_reread(self):
        from types import SimpleNamespace
        from PIL import Image
        calls = []
        def engine(image):
            calls.append(1)
            return SimpleNamespace(txts=('1000[10.00%]',), scores=(.99,))
        reader = tracker.LineReader(engine)
        image = Image.new('RGB', (100, 20), '#444444')
        self.assertFalse(reader.read(image, 0)[3])
        self.assertEqual(len(calls), 2)
        self.assertTrue(reader.read(image.copy(), 1)[3])
        self.assertEqual(len(calls), 2)
        self.assertFalse(reader.read(image, 5)[3])
        image.putpixel((1, 1), (255, 255, 255))
        self.assertFalse(reader.read(image, 6)[3])
        self.assertEqual(len(calls), 6)

    def test_high_cpu_still_captures_instead_of_skipping_indefinitely(self):
        from types import SimpleNamespace
        import queue
        from PIL import Image
        class StopAfterCycle:
            done = False
            def is_set(self): return self.done
            def wait(self, seconds):
                if seconds == .15: return False
                self.done = True; return True
        fake = SimpleNamespace(running=True, generation=1, region={'left':0,'top':0,'width':100,'height':20},
                               engine=lambda image: SimpleNamespace(txts=('1000[10.00%]',),scores=(.99,)),
                               stop=StopAfterCycle(), events=queue.Queue(), scan_seconds=0, target_seconds=1, session=tracker.Session())
        process = SimpleNamespace(nice=lambda value: None, cpu_times=lambda: (0,0))
        with patch('psutil.Process', return_value=process), patch('psutil.cpu_percent', return_value=99), patch.object(tracker,'capture_image',return_value=test_image()):
            tracker.App.worker(fake)
        event = fake.events.get_nowait()
        self.assertEqual(event[1], (1000,10))
        self.assertIsNone(event[4])
        self.assertLessEqual(fake.target_seconds, 5)

    def test_third_preprocessing_rescues_two_matching_candidates(self):
        from types import SimpleNamespace
        from PIL import Image
        calls=[]
        def engine(image):
            texts=('1000[10.00%]','1001[10.01%]','1000[10.00X]')
            text=texts[len(calls)];calls.append(1)
            return SimpleNamespace(txts=(text,),scores=(.99,))
        reader=tracker.LineReader(engine)
        self.assertEqual(reader.read(test_image(),0)[0],(1000,10))
        self.assertEqual(len(calls),3)

    def test_disagreed_ocr_is_never_cached(self):
        from types import SimpleNamespace
        from PIL import Image
        calls = []
        def engine(image):
            calls.append(1)
            text = ('1000[10.00%]','1001[10.01%]','1002[10.02%]')[(len(calls)-1)%3]
            return SimpleNamespace(txts=(text,), scores=(.99,))
        reader = tracker.LineReader(engine)
        image = Image.new('RGB', (100, 20), '#444444')
        self.assertIsNone(reader.read(image, 0)[0])
        self.assertIsNone(reader.read(image, 1)[0])
        self.assertEqual(len(calls), 6)

    def test_ambiguous_one_exp_cannot_start_timer_but_exact_one_exp_can(self):
        from types import SimpleNamespace
        from PIL import Image
        outputs = iter((('360900567[88.93%]',.96), ('360900568[88.93%]]',.99), ('unreadable',.99)))
        def engine(image):
            text,score=next(outputs)
            return SimpleNamespace(txts=(text,),scores=(score,))
        reader = tracker.LineReader(engine)
        s=tracker.Session(level=169)
        establish(s,(360900567,88.93),0)
        image=Image.new('RGB',(181,22),'#444444')
        value=reader.read(image,1)[0]
        self.assertIsNone(value)
        self.assertIsNone(s.started)
        self.assertEqual(s.elapsed(120),0)
        self.assertEqual(len(reader.last_readings),3)
        exact=tracker.LineReader(lambda image: SimpleNamespace(txts=('360900568[88.93%]',),scores=(.99,)))
        s.accept(exact.read(image,2)[0],2)
        self.assertEqual(s.started,2)
        self.assertEqual(s.stats(10)[0],1)


if __name__ == '__main__':
    unittest.main()


def test_image():
    from PIL import Image
    image=Image.new('RGB',(100,20),'#444444')
    image.putpixel((1,1),(255,255,255))
    return image

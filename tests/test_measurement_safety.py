import csv
import json
import queue
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tracker


def establish(session, value, stamp=0):
    session.accept(value, stamp-1)
    session.accept(value, stamp)


class MeasurementSafetyTests(unittest.TestCase):
    def test_wrong_first_frame_is_replaced_before_baseline_is_committed(self):
        for wrong in (172670132, 172690132):
            with self.subTest(wrong=wrong):
                s = tracker.Session(level=169)
                s.accept((wrong, 42.55), 0)
                self.assertIsNone(s.first)
                self.assertEqual(s.rows, [])
                s.accept((172680132, 42.55), 1)
                self.assertIsNone(s.first)
                s.accept((172680132, 42.55), 2)
                self.assertEqual(s.first, (172680132, 42.55))
                s.accept((172680133, 42.55), 3)
                self.assertEqual(s.stats(10)[0], 1)
                self.assertEqual(s.started, 3)

    def test_duplicate_timestamps_and_rapid_callbacks_do_not_confirm_baseline(self):
        s = tracker.Session(level=169)
        for stamp in (0, 0, .1, .2):
            s.accept((172680132, 42.55), stamp)
        self.assertIsNone(s.first)
        s.accept((172680132, 42.55), 1)
        self.assertEqual(s.first, (172680132, 42.55))
        self.assertIsNone(s.started)

    def test_changing_or_stale_baseline_waits_instead_of_inventing_gain(self):
        s = tracker.Session(level=169)
        s.accept((172680132, 42.55), 0)
        s.accept((172680132, 42.55), 20)
        self.assertIsNone(s.first)
        s.accept((172680142, 42.55), 21)
        self.assertIsNone(s.first)
        self.assertEqual(s.elapsed(1000), 0)
        s.accept((172680142, 42.55), 22)
        self.assertEqual(s.first[0], 172680142)

    def test_different_valid_decrease_values_trigger_a_safe_stop(self):
        s = tracker.Session(level=169)
        establish(s, (245596661, 60.52))
        for stamp, exp in enumerate((235596661, 235606661, 235616661), 1):
            s.accept((exp, round(exp/s.total*100, 2)), stamp)
        self.assertTrue(s.blocked)
        self.assertEqual(s.last[0], 245596661)
        self.assertEqual(s.stats(10)[0], 0)
        self.assertIn('감소', s.stop_reason)

    def test_persistent_level_mismatch_keeps_sampling_and_recovers_without_fake_gain(self):
        s = tracker.Session(level=169)
        exp = 400000000
        establish(s, (exp, round(exp/s.total*100, 2)))
        state = ''
        for stamp, wrong_exp in enumerate((10000000, 10010000, 10020000, 10030000), 1):
            state = s.accept((wrong_exp, round(wrong_exp/tracker.EXP_TABLE[170]*100, 2)), stamp)
        self.assertFalse(s.blocked)
        self.assertEqual(s.decrease_count, 0)
        self.assertEqual(s.last[0], exp)
        self.assertIn('마지막 정상값 유지', state)
        self.assertEqual(s.mismatch_count, 4)
        recovered = 400000100
        s.accept((recovered, round(recovered/s.total*100, 2)), 5)
        self.assertFalse(s.blocked)
        self.assertEqual(s.last[0], recovered)
        self.assertEqual(s.mismatch_count, 0)

    def test_one_mismatch_then_a_valid_reading_recovers_normally(self):
        s = tracker.Session(level=169)
        establish(s, (172680132, 42.55))
        s.accept((172680142, 43.55), 1)
        s.accept((172680142, 42.55), 2)
        self.assertFalse(s.blocked)
        self.assertEqual(s.stats(10)[0], 10)

    def test_long_gap_leaves_unknown_minutes_and_retains_last_valid_window(self):
        s = tracker.Session()
        establish(s, (1000, 10))
        for stamp in range(2, 63, 2):
            exp = 1000+stamp*5
            s.accept((exp, exp/100), stamp)
        first = s.valid_minute['one'].copy()
        s.accept((2210, 22.1), 242)
        self.assertIsNone(s.minute_snapshots[1]['cumulative'])
        self.assertIsNone(s.minute_snapshots[2]['one'])
        self.assertIsNone(s.minute_result()['one'])
        self.assertEqual(s.valid_minute['one'], first)
        # Internal gaps do not invalidate a six-minute total whose endpoints are known.
        self.assertEqual(s.valid_minute['six']['six'], 1210)
        self.assertEqual(s.stats(242)[0], 1210)
        for stamp in range(244, 303, 2):
            exp = 1000+stamp*5
            s.accept((exp, exp/100), stamp)
        self.assertEqual(s.valid_minute['one']['index'], 5)
        self.assertEqual(s.valid_minute['one']['one'], 300)

    def test_stop_checkpoint_restores_elapsed_and_average_instead_of_last_reading_time(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            s = tracker.Session(level=169)
            with patch.object(tracker.time, 'time', return_value=2000000), patch.object(tracker.time, 'monotonic', return_value=0):
                establish(s, (172680132, 42.55))
                s.accept((172680232, 42.55), 2)
                s.accept((172680332, 42.55), 12)
            app = tracker.App.__new__(tracker.App)
            app.session, app.running, app.generation, app.save_warning = s, True, 0, ''
            with patch.object(tracker.time, 'monotonic', return_value=32):
                app.finish_measurement('사용자 정지')
            restored = tracker.load_saved_session(Path(folder)/'records'/(s.session_id+'.csv'), 169, 1000)
            self.assertEqual(restored.elapsed(2000), 30)
            self.assertEqual(restored.stats(2000)[0], 200)
            self.assertEqual(restored.stats(2000)[1]/60, 400)
            self.assertEqual(restored.stop_reason, '사용자 정지')
            self.assertTrue(restored.restored)
            # Wall-clock adjustments must not override the saved monotonic hunting duration.
            path = Path(folder)/'records'/(s.session_id+'.csv')
            with path.open(encoding='utf-8-sig') as stream:
                rows = list(csv.reader(stream))
            rows[-1][0] = (datetime.fromisoformat(rows[-1][0])+timedelta(minutes=1)).isoformat(sep=' ')
            with path.open('w', encoding='utf-8-sig', newline='') as stream:
                csv.writer(stream).writerows(rows)
            restored = tracker.load_saved_session(path, 169, 2000)
            self.assertEqual(restored.elapsed(3000), 30)
            self.assertEqual(restored.stats(3000)[1]/60, 400)

    def test_inconsistent_summary_cannot_override_the_observation_ledger(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            app = tracker.App.__new__(tracker.App)
            app.session = tracker.Session(level=169)
            establish(app.session, (172680132, 42.55))
            app.session.accept((172680232, 42.55), 2)
            with patch.object(tracker.time, 'monotonic', return_value=2):
                app.save_observation()
            path = next((Path(folder)/'records').glob('*.json'))
            state = json.loads(path.read_text())
            state.update(elapsed_seconds=999, samples=999)
            path.write_text(json.dumps(state))
            restored = tracker.load_saved_session(path.with_suffix('.csv'), 169, 1000)
            self.assertEqual(restored.elapsed(1000), 0)
            self.assertEqual(restored.stats(1000)[0], 100)

    def test_backward_wall_clock_while_waiting_cannot_create_future_minute_buckets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'session.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['관측 시각','현재 경험치','퍼센트','사냥 초','총 획득 경험치','상태'])
                writer.writerows([
                    ['2026-10-31 10:02:00', 172680132, 42.55, 0, 0, 'waiting'],
                    ['2026-10-31 10:00:00', 172686132, 42.55, 0, 6000, 'hunting'],
                    ['2026-10-31 10:01:00', 172692132, 42.55, 60, 12000, 'hunting']])
            path.with_suffix('.json').write_text(json.dumps({'version':4, 'samples':3,
                'baseline':[172680132,42.55], 'last':[172692132,42.55], 'elapsed_seconds':60}))
            restored = tracker.load_saved_session(path, 169, 1000)
            self.assertEqual(restored.elapsed(1000), 60)
            self.assertEqual(restored.stats(1000)[0], 12000)
            self.assertEqual(restored.minute_result()['index'], 1)
            self.assertEqual(restored.valid_minute['one']['one'], 12000)
            self.assertEqual(restored.valid_minute['six']['six'], 12000)

    def test_failed_csv_append_is_repaired_without_dropping_or_duplicating_rows(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            app = tracker.App.__new__(tracker.App)
            app.session = tracker.Session()
            establish(app.session, (1000, 10))
            app.save_observation()
            app.session.accept((1010, 10.1), 2)
            original_open = Path.open
            def failing_open(path, mode='r', *args, **kwargs):
                if path.suffix == '.csv' and mode == 'a':
                    with original_open(path, 'a') as stream:
                        stream.write('partial')
                    raise OSError('injected partial append')
                return original_open(path, mode, *args, **kwargs)
            with patch.object(Path, 'open', failing_open):
                with self.assertRaises(OSError):
                    app.save_observation()
            app.session.accept((1020, 10.2), 3)
            app.save_observation()
            with next((Path(folder)/'records').glob('*.csv')).open(encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([int(row['현재 경험치']) for row in rows], [1000, 1010, 1020])

    def test_initialization_failure_retries_the_worker_and_produces_a_valid_capture(self):
        from PIL import Image
        class Stop:
            done = False
            retries = 0
            def is_set(self): return self.done
            def wait(self, duration):
                if duration == .15: return False
                if duration == 2:
                    self.retries += 1
                    self.done = self.retries >= 2
                else:
                    self.done = True
                return self.done
        stop = Stop()
        fake = SimpleNamespace(running=True, generation=1, region={'left':0,'top':0,'width':100,'height':20},
            engine=lambda image:SimpleNamespace(txts=('1000[10.00%]',),scores=(.99,)),
            stop=stop, events=queue.Queue(), scan_seconds=0, target_seconds=1, session=tracker.Session())
        process = SimpleNamespace(nice=lambda value:None, cpu_times=lambda:(0,0))
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)), \
                patch('psutil.Process', side_effect=[OSError('injected initialization failure'), process]), \
                patch('psutil.cpu_percent', return_value=0), \
                patch.object(tracker, 'capture_image', return_value=test_image()):
            tracker.App.worker(fake)
        error, success = fake.events.get_nowait(), fake.events.get_nowait()
        self.assertTrue(error[6]['initialization_error'])
        self.assertEqual(success[1], (1000, 10))
        self.assertEqual(stop.retries, 1)

    def test_failed_final_save_preserves_session_and_blocks_reset_level_change_and_close(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            for action in ('reset', 'change_level', 'shutdown'):
                with self.subTest(action=action):
                    import qt_controller
                    app = qt_controller.SharedController.__new__(qt_controller.SharedController)
                    app.key_repeat = SimpleNamespace(shutdown=lambda: None)
                    app.session = tracker.Session(level=169)
                    establish(app.session, (172680132, 42.55))
                    app.session.accept((172680232, 42.55), 2)
                    app.running, app.generation = True, 0
                    app.status = SimpleNamespace(set=lambda text: None)
                    app.level_var = SimpleNamespace(get=lambda:170, set=lambda level:None)
                    identity, rows = app.session.session_id, list(app.session.rows)
                    with patch.object(app, 'save_observation', side_effect=OSError('injected save failure')):
                        getattr(app, action)()
                    self.assertFalse(app.running)
                    self.assertEqual(app.session.session_id, identity)
                    self.assertEqual(app.session.rows, rows)
                    self.assertEqual(app.session.level, 169)
                    self.assertEqual(app.session.stats(10)[0], 100)
                    self.assertIn('저장 실패', app.save_warning)

    def test_pending_baseline_forces_real_ocr_even_for_identical_pixels(self):
        from PIL import Image
        class Stop:
            cycles = 0
            def is_set(self): return self.cycles >= 2
            def wait(self, duration):
                if duration == .15: return False
                self.cycles += 1; return self.is_set()
        calls = []
        def engine(image):
            calls.append(1)
            return SimpleNamespace(txts=('1000[10.00%]',), scores=(.99,))
        fake = SimpleNamespace(running=True, generation=1, region={'left':0,'top':0,'width':100,'height':20},
            engine=engine,stop=Stop(),events=queue.Queue(),scan_seconds=0,target_seconds=1,session=tracker.Session())
        process = SimpleNamespace(nice=lambda value:None,cpu_times=lambda:(0,0))
        with patch('psutil.Process',return_value=process), patch('psutil.cpu_percent',return_value=0), \
                patch.object(tracker,'capture_image',return_value=test_image()):
            tracker.App.worker(fake)
        self.assertEqual(len(calls), 12)
        self.assertFalse(fake.events.get_nowait()[6]['cached'])
        self.assertFalse(fake.events.get_nowait()[6]['cached'])

    def test_recent_update_handoff_preserves_baseline_and_counts_time_during_restart(self):
        boot_path = Path('/proc/sys/kernel/random/boot_id')
        if not boot_path.exists(): self.skipTest('Linux update handoff')
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            app = tracker.App.__new__(tracker.App)
            app.session = tracker.Session(level=169)
            with patch.object(tracker.time, 'time', return_value=2000000), patch.object(tracker.time, 'monotonic', return_value=0):
                establish(app.session, (172680132, 42.55))
                app.session.accept((172680232, 42.55), 2)
                app.session.accept((172680332, 42.55), 12)
            app.session.finish(32, 'update')
            app.save_observation()
            path = Path(folder)/'handoff.json'
            data = {'session_id':app.session.session_id, 'baseline':list(app.session.first),
                    'running_before_update':True, 'snapshot_monotonic':32, 'boot_id':boot_path.read_text().strip()}
            path.write_text(json.dumps(data))
            app.running = False
            app.status = SimpleNamespace(set=lambda text:None)
            with patch.object(tracker.time, 'monotonic', return_value=35):
                self.assertTrue(app.resume_handoff(path))
            self.assertTrue(app.running)
            self.assertEqual(app.session.first, (172680132, 42.55))
            self.assertEqual(app.session.elapsed(35), 33)
            app.session.accept((172680432, 42.55), 36)
            self.assertEqual(app.session.stats(36)[0], 300)
            self.assertEqual(app.session.elapsed(36), 34)
            app.running = False
            with patch.object(tracker.time, 'monotonic', return_value=1000):
                self.assertFalse(app.resume_handoff(path))
            self.assertFalse(app.running)


if __name__ == '__main__':
    unittest.main()


def test_image():
    from PIL import Image
    image=Image.new('RGB',(100,20),'#444444')
    image.putpixel((1,1),(255,255,255))
    return image

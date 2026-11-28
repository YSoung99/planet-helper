"""Comprehensive regression, concurrency, and stress tests for v1.4.42 stability & optimizations."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

import exp_buffs
import jev
import jev_assist
import map_name
import meso
import potions
import record_history
import records_backup
import segments
import tracker
from mac_app import Controller


class Phase1CriticalBugTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_controller(self, tmp):
        c = Controller(self.app, start_worker=False, restore=False)
        c.timer.stop()
        return c

    def _cleanup_controller(self, c):
        c.key_repeat.shutdown()
        c.start_captures.close()
        c._shutdown_probes()
        c.window.hide()
        c.window.deleteLater()
        self.app.processEvents()

    def test_p1_1_ended_session_meso_marks_survive_next_start_and_do_not_repush_cloud(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.cloud_remote = 'gdrive:planet-helper'
                c.running = True
                c.session = tracker.Session(level=170)
                for stamp, exp in ((0.0, 219671043), (1.0, 219671043), (2.0, 219676043), (62.0, 219686043)):
                    c.session.accept((exp, 51.32), stamp, corroborated=True, baseline_verified=True)
                c.session.meso.observe(5_000_000, active_seconds=0.0)
                c.session.meso.observe(2_000_000, threshold=500_000, active_seconds=30.0)
                c.session.meso.complete = True
                c.session.meso_dirty = True
                with patch.object(c, 'start_cloud_sync') as push:
                    self.assertTrue(c.finish_measurement('사용자 정지'))
                    self.assertEqual(push.call_count, 1)
                    sid = c.session.session_id
                    record_history.save_meso_marks(sid, {0})
                    # Starting a new measurement calls finish_measurement again on the already-ended session.
                    self.assertTrue(c.reset())
                    self.assertEqual(push.call_count, 1)  # never re-pushes an already-ended session
                saved = json.loads((Path(tmp) / 'records' / f'{sid}.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['meso']['marked'], [0])
            finally:
                self._cleanup_controller(c)

    def test_p1_2_choose_character_while_stopped_does_not_overwrite_ended_record(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.current_character = '본캐'
                c.running = True
                c.session = tracker.Session(level=170)
                for stamp, exp in ((0.0, 219671043), (1.0, 219671043), (2.0, 219676043)):
                    c.session.accept((exp, 51.32), stamp, corroborated=True, baseline_verified=True)
                c.finish_measurement('사용자 정지')
                sid = c.session.session_id
                with patch('PySide6.QtWidgets.QInputDialog.getItem', return_value=('부캐', True)):
                    c.choose_character()
                self.assertEqual(c.current_character, '부캐')
                saved = json.loads((Path(tmp) / 'records' / f'{sid}.json').read_text(encoding='utf-8'))
                self.assertEqual(saved.get('character_assigned'), '본캐')
            finally:
                self._cleanup_controller(c)

    def test_p1_3_load_saved_session_preserves_metadata_when_json_and_csv_rows_drift(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            session = tracker.Session(level=170)
            for stamp, exp in ((0.0, 219671043), (1.0, 219671043), (2.0, 219676043), (62.0, 219686043), (122.0, 219696043)):
                session.accept((exp, 51.32), stamp, corroborated=True, baseline_verified=True)
            session.character_name = '본캐'
            session.exp_buff_intervals = {'coupon': [[0, 60]]}
            session.meso.observe(10_000_000, active_seconds=0.0)
            session.meso.observe(10_500_000, active_seconds=120.0)
            session.meso.complete = True
            session.potions.observe('쭈쭈바', 500, unit_cost=2300)
            session.potions.observe('쭈쭈바', 480, unit_cost=2300)
            writer = tracker.App.__new__(tracker.App)
            writer.session = session
            with patch.object(tracker.time, 'monotonic', return_value=122.0):
                writer.save_observation(force_summary=True)
            csv_path = Path(tmp) / 'records' / f'{session.session_id}.csv'
            # Simulate crash where CSV lost 2 trailing rows relative to JSON summary.
            lines = csv_path.read_text(encoding='utf-8-sig').splitlines()
            csv_path.write_text('\n'.join(lines[:-2]) + '\n', encoding='utf-8-sig')

            restored = tracker.load_saved_session(csv_path, 170, 200.0)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.character_name, '본캐')
            self.assertEqual(restored.meso.hunted(), 500_000)
            self.assertEqual(restored.potions.total_used(), 20)
            self.assertEqual(restored.exp_buff_intervals, {'coupon': [[0, 60]]})
            summary_row = record_history.summarize(csv_path)
            self.assertEqual(summary_row['meso'], 500_000)
            self.assertEqual(summary_row['potions']['total_used'], 20)

    def test_p1_4_potion_reader_keys_by_slot_identity_not_list_index(self):
        def make_tile(color, label):
            tile = Image.new('RGB', (48, 48), (220, 220, 220))
            d = ImageDraw.Draw(tile)
            d.ellipse((10, 12, 38, 36), fill=color, outline=(20, 20, 20))
            d.text((2, 2), label, fill=(255, 255, 255))
            return tile

        with tempfile.TemporaryDirectory() as tmp:
            icon_a = potions.save_icon(make_tile((220, 30, 30), 'A'), base=tmp)
            icon_b = potions.save_icon(make_tile((30, 60, 220), 'B'), base=tmp)
            frame = Image.new('RGB', (1000, 600), (20, 25, 30))
            frame.paste(make_tile((220, 30, 30), 'A'), (750, 500))
            frame.paste(make_tile((30, 60, 220), 'B'), (830, 500))
            slot_a = {'name': '빨포', 'unit_cost': 100, 'icon': icon_a, 'reference_height': 600}
            slot_b = {'name': '파포', 'unit_cost': 200, 'icon': icon_b, 'reference_height': 600}

            def fake_ocr(img):
                # Distinguish slot A vs slot B by image mean color (red vs blue doesn't reach digit strip,
                # so check call count order).
                return [('111', 0.98)] if calls.append(1) is None and len(calls) == 1 else [('222', 0.98)]

            calls = []
            reader = potions.Reader(ocr_fn=fake_ocr, base=tmp)
            self.assertEqual(reader.read_slots(frame, [slot_a, slot_b]), [('빨포', 111), ('파포', 222)])
            # Delete slot_a so slot_b shifts from index 1 -> index 0!
            # Must still read slot_b's own cached value (222), NEVER slot_a's (111)!
            self.assertEqual(reader.read_slots(frame, [slot_b]), [('파포', 222)])

    def test_p1_5_potion_digit_drop_guard_and_misread_rollback(self):
        conf = potions.SlotConfirmation()
        self.assertIsNone(conf.observe(250, 1.0))
        self.assertEqual(conf.observe(250, 3.0), 250)
        # 250 -> 25 is a digit drop: needs 3 sightings, not 2.
        self.assertIsNone(conf.observe(25, 5.0))
        self.assertIsNone(conf.observe(25, 7.0))
        self.assertEqual(conf.observe(25, 9.0), 25)

        sp = potions.SessionPotions()
        sp.observe('쭈쭈바', 250, unit_cost=2300, stamp=3.0)
        sp.observe('쭈쭈바', 25, unit_cost=2300, stamp=9.0)
        self.assertEqual(sp.total_used(), 225)
        # 10s later OCR recovers to 249 -> bogus drop rolled back to used=1, restocks=0!
        sp.observe('쭈쭈바', 249, unit_cost=2300, stamp=19.0)
        self.assertEqual(sp.total_used(), 1)
        self.assertEqual(sp.items['쭈쭈바']['restocks'], 0)
        # Real restock above pre-drop value (249 -> 649) still counts as a restock.
        sp.observe('쭈쭈바', 649, unit_cost=2300, stamp=25.0)
        self.assertEqual(sp.total_used(), 1)
        self.assertEqual(sp.items['쭈쭈바']['restocks'], 1)

    def test_p0_jev1_exp_buffs_inactive_and_scale_lock_and_batched_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            icon = Image.new('RGB', (37, 37), (37, 61, 79))
            d = ImageDraw.Draw(icon)
            d.ellipse((4, 4, 28, 20), fill=(242, 40, 13))
            d.line((7, 21, 27, 6), fill=(51, 234, 157), width=4)
            source = root / 'icon.png'
            icon.save(source)
            exp_buffs.register(root, 'coupon', source)

            # Non-blank textured frame WITHOUT the buff icon -> local 'inactive' (0 JEV calls!)
            textured = Image.new('RGB', (1050, 750), (30, 50, 70))
            td = ImageDraw.Draw(textured)
            for x in range(440, 1040, 20):
                td.rectangle((x, 45, x + 10, 195), fill=(80, 120, 160))
            detector = exp_buffs.Detector()
            self.assertEqual(detector.read(textured, root)['coupon'], 'inactive')

            # Scale lock engages on match and auto-unlocks after 5 consecutive misses
            with_buff = textured.copy()
            with_buff.paste(icon, (982, 88))
            self.assertEqual(detector.read(with_buff, root)['coupon'], 'active')
            self.assertIn(('coupon', with_buff.size), detector.locked_scales)
            for i in range(exp_buffs.SCALE_UNLOCK_MISSES):
                miss = textured.copy()
                miss.putpixel((500 + i, 60), (255, 255, 255))
                self.assertEqual(detector.read(miss, root)['coupon'], 'inactive')
            self.assertNotIn(('coupon', with_buff.size), detector.locked_scales)

            # Batched JEV call + BUFF_ACTIVE_TTL cap
            with patch.object(tracker, 'BASE', root):
                exp_buffs.register(root, 'holy_symbol', source)
                c = self._make_controller(tmp)
                try:
                    c.jev_enabled = True
                    c.jev_arbiter = jev.Arbiter(lambda: True, cooldown=0, runner=lambda w: w())
                    both_unknown = {'coupon': 'unknown', 'holy_symbol': 'unknown'}
                    with patch('jev_assist.ask_buffs', return_value={'coupon': 'active', 'holy_symbol': 'inactive'}) as batch:
                        c.jev_buff_readings(textured, 10.0, both_unknown)
                        res_soon = c.jev_buff_readings(textured, 15.0, both_unknown)
                        res_late = c.jev_buff_readings(textured, 55.0, both_unknown)
                    batch.assert_called_once()
                    self.assertEqual(res_soon, {'coupon': 'active', 'holy_symbol': 'inactive'})
                    # After 30s TTL, 'active' is no longer smeared across the rest of the 120s bucket!
                    self.assertEqual(res_late['coupon'], 'unknown')
                    self.assertEqual(res_late['holy_symbol'], 'inactive')
                finally:
                    self._cleanup_controller(c)

    def test_p0_jev2_meso_change_uses_exact_change_frame_not_latest(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = self._make_controller(tmp)
            try:
                c.jev_enabled = True
                c.running = True
                c.jev_arbiter = jev.Arbiter(lambda: True, cooldown=0, runner=lambda w: w())
                c.session.meso_start_at = 100.0
                red = Image.new('RGB', (640, 360), (220, 20, 20))
                blue = Image.new('RGB', (640, 360), (20, 20, 220))
                c._last_meso_frame = (c.generation, 105.0, red)
                seen = []
                def fake_ask(frame, delta):
                    seen.append(frame.getpixel((10, 10)))
                    return '거래·구매·창고'
                with patch('jev_assist.ask_meso_change', side_effect=fake_ask):
                    for stamp, val in ((101.0, 5_000_000), (103.0, 5_000_000),
                                       (105.0, 2_000_000), (107.0, 2_000_000), (109.0, 2_000_000)):
                        c.meso_results.put((c.generation, 1, stamp, '본캐', val))
                        c.apply_meso_results(stamp)
                    # Later blue frame enters jev_frames; verify saved snapshot for change #0 is still red.
                    c.jev_frames.remember(300.0, blue, force=True)
                    c.jev_arbiter.answers.clear()
                    c.meso_change_suggestion(0, -3_000_000)
                self.assertGreaterEqual(len(seen), 1)
                for r, g, b in seen:
                    self.assertGreater(r, 180)
                    self.assertLess(b, 60)
            finally:
                self._cleanup_controller(c)


class Phase2OptimizationAndStressTests(unittest.TestCase):
    def test_shared_ocr_engine_reused_by_potions_and_map_name_under_concurrency(self):
        calls = {'medium': 0, 'fast': 0}
        active = {'medium': 0, 'fast': 0}
        overlap = []

        def make_fn(kind, text):
            def fn(rgb, **kwargs):
                active[kind] += 1
                if active[kind] > 1:
                    overlap.append(kind)
                calls[kind] += 1
                active[kind] -= 1
                return SimpleNamespace(txts=(text,), scores=(0.98,))
            return fn

        two_tier = tracker.TwoTierOcr(make_fn('medium', '322'), make_fn('fast', '12345'))
        prev = tracker.shared_ocr_engine()
        tracker.register_shared_engine(two_tier)
        try:
            eng, lock = potions._get_rec_engine()
            self.assertIs(eng, two_tier.medium)
            self.assertIs(lock, two_tier.medium_lock)
            self.assertIs(map_name._get_digit_engine(), two_tier.fast)

            strip = Image.new('RGB', (40, 18), (50, 50, 50))
            errors = []
            def worker():
                try:
                    for _ in range(15):
                        self.assertEqual(potions.recognize_count(strip), 322)
                        lines = map_name.recognize_lines(strip, detect=False, digits_only=True)
                        self.assertEqual(lines[0][0], '12345')
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            self.assertEqual(overlap, [])
        finally:
            tracker.register_shared_engine(prev)

    def test_potion_miss_cache_skips_repeat_full_search_on_unchanged_frame(self):
        tile = Image.new('RGB', (48, 48), (220, 220, 220))
        ImageDraw.Draw(tile).ellipse((10, 12, 38, 36), fill=(220, 30, 30))
        with tempfile.TemporaryDirectory() as tmp:
            icon = potions.save_icon(tile, base=tmp)
            slot = {'name': '없는포션', 'unit_cost': 0, 'icon': icon, 'reference_height': 600}
            empty = Image.new('RGB', (1000, 600), (20, 30, 40))
            reader = potions.Reader(ocr_fn=lambda img: [('99', 0.99)], base=tmp)
            with patch('potions.find_icon', wraps=potions.find_icon) as spy:
                self.assertEqual(reader.read_slots(empty, [slot]), [('없는포션', None)])
                self.assertEqual(reader.read_slots(empty, [slot]), [('없는포션', None)])
                self.assertEqual(reader.read_slots(empty, [slot]), [('없는포션', None)])
                self.assertEqual(spy.call_count, 1)

    def test_jev_ask_many_and_arbiter_priority_and_jpeg_encoding(self):
        big = Image.new('RGB', (1280, 720), (40, 60, 80))
        small = Image.new('RGB', (120, 24), (40, 60, 80))
        self.assertTrue(jev.image_url(big).startswith('data:image/jpeg;base64,'))
        self.assertTrue(jev.image_url(small).startswith('data:image/png;base64,'))

        reply = {'answers': [
            {'name': 'coupon', 'probabilities': [{'value': 'active', 'probability': 0.96}]},
            {'name': 'holy_symbol', 'probabilities': [{'value': 'inactive', 'probability': 0.94}]},
        ]}
        with patch('map_name.gateway_key', return_value='k'), patch('jev.request', return_value=reply) as req:
            out = jev.ask_many(
                [('bar', big)],
                [('coupon', 'q1', [('active', 'y'), ('inactive', 'n')]),
                 ('holy_symbol', 'q2', [('active', 'y'), ('inactive', 'n')])],
            )
            self.assertEqual(out, {'coupon': 'active', 'holy_symbol': 'inactive'})
            req.assert_called_once()

        clock = [100.0]
        arbiter = jev.Arbiter(cooldown=10.0, clock=lambda: clock[0], runner=lambda w: w())
        self.assertTrue(arbiter.submit('low', lambda: 'ok', priority=0))
        clock[0] += 3.5
        self.assertFalse(arbiter.submit('low2', lambda: 'ok', priority=0))
        # High-priority OCR tie-break proceeds after 3s instead of waiting full 10s cooldown.
        self.assertTrue(arbiter.submit('urgent', lambda: 'ok', priority=2))

    def test_gateway_key_cache_and_invalidation(self):
        map_name.invalidate_key_cache()
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(stdout='cached-secret\n', stderr='', returncode=0)
        with patch.dict(os.environ, {'LLM_GATEWAY_API_KEY': ''}, clear=False), \
                patch('map_name.subprocess.run', side_effect=fake_run):
            self.assertEqual(map_name.gateway_key(), 'cached-secret')
            self.assertEqual(map_name.gateway_key(), 'cached-secret')
            self.assertEqual(len(calls), 1)
            map_name.store_key('new-secret')
            self.assertEqual(map_name.gateway_key(), 'cached-secret')
            self.assertEqual(len(calls), 3)  # lookup + store + fresh lookup after invalidation
        map_name.invalidate_key_cache()

    def test_records_backup_stalls_exclude_active_partial_upgrade_and_concurrent_writes(self):
        try:
            from test_records_features import write_record
        except ModuleNotFoundError:
            from tests.test_records_features import write_record
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a', Path(tmp) / 'b'
            sid1 = write_record(a, character='본캐', gain=5000)
            sid2 = write_record(a, character='본캐', gain=7000)
            jev_assist.save_stall(sid1, 3, '사망', a)

            # Exclude active session sid2 from export
            z1 = Path(tmp) / 'z1.zip'
            self.assertEqual(records_backup.export_zip(z1, a, exclude_sessions={sid2}), 1)
            added, skipped, names = records_backup.import_zip(z1, b)
            self.assertEqual(added, 1)
            self.assertEqual(jev_assist.load_stalls(b)[sid1], {'3': '사망'})
            self.assertFalse((b / 'records' / f'{sid2}.csv').exists())

            # Partial CSV on b gets upgraded when a has a strict continuation
            csv_b = b / 'records' / f'{sid1}.csv'
            full_bytes = (a / 'records' / f'{sid1}.csv').read_bytes()
            csv_b.write_bytes(full_bytes[:len(full_bytes) // 2])
            z2 = Path(tmp) / 'z2.zip'
            records_backup.export_zip(z2, a)
            added2, skipped2, _ = records_backup.import_zip(z2, b)
            self.assertEqual(added2, 2)  # sid1 upgraded + sid2 newly added
            self.assertEqual(csv_b.read_bytes(), full_bytes)

            # Concurrent shared JSON writes across 6 threads never corrupt files
            errors = []
            def stress(tid):
                try:
                    for i in range(25):
                        record_history.save_memo(f's-{tid}-{i}', f'맵{tid}-{i}', b)
                        segments.save_tag(f's-{tid}', i, f'태그{i}', b)
                        jev_assist.save_stall(f's-{tid}', i, '사망', b)
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=stress, args=(t,)) for t in range(6)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])
            memos = record_history.load_memos(b)
            self.assertEqual(len([k for k in memos if k.startswith('s-')]), 150)

    def test_meso_checkpoint_thinning_preserves_recent_flow_window_under_stress(self):
        sm = meso.SessionMeso()
        for sec in range(0, 3000, 2):  # 1,500 checkpoints (> MAX_CHECKPOINTS=600)
            sm.observe(10_000_000 + sec * 100, active_seconds=float(sec))
        self.assertLessEqual(len(sm.checkpoints), meso.MAX_CHECKPOINTS + 2)
        times = [t for t, _ in sm.checkpoints]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), len(set(times)))  # zero duplicate timestamps
        recent = [t for t in times if t >= times[-1] - meso.RECENT_FLOW_WINDOW]
        # Recent 6-minute window must remain high resolution (>= 100 points)
        self.assertGreaterEqual(len(recent), 100)

    def test_restore_latest_record_skips_meso_csv(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            rec = Path(tmp) / 'records'
            rec.mkdir()
            (rec / 'meso.csv').write_text('시각,캐릭터,메소\n2026-11-29,본캐,1000\n', encoding='utf-8-sig')
            app = tracker.App.__new__(tracker.App)
            app.session = tracker.Session(level=170)
            app.value = app.status = SimpleNamespace(set=lambda *_: None)
            with patch('tracker.load_saved_session', side_effect=AssertionError('should skip meso.csv')):
                app.restore_latest_record()

    def test_auto_measure_ends_when_observations_stop_for_five_minutes(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp, patch.object(tracker, 'BASE', Path(tmp)):
            c = Controller(app, start_worker=False, restore=False)
            c.timer.stop()
            try:
                c.auto_measure = True
                c.running = True
                c.session = tracker.Session(level=170)
                for stamp, exp in ((0.0, 219671043), (1.0, 219671043), (2.0, 219676043)):
                    c.session.accept((exp, 51.32), stamp, corroborated=True, baseline_verified=True)
                # Window minimized at t=2.0 -> no new samples arrive -> at t=305.0 auto_measure ends!
                c.auto_measure_tick(305.0)
                self.assertFalse(c.running)
                self.assertIn('자동 종료', c.status.get())
            finally:
                c.key_repeat.shutdown()
                c.start_captures.close()
                c._shutdown_probes()
                c.window.hide()
                c.window.deleteLater()
                app.processEvents()


if __name__ == '__main__':
    unittest.main()

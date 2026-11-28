"""Tests for OCR/capture optimizations, history caching/filters, JEV controls and update pruning."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from PySide6.QtWidgets import QApplication

import auto_hud
import jev
import linux_updates
import native_ocr
import record_history
import tracker
import window_capture
from mac_app import Controller


class NativeOcrLookupTests(unittest.TestCase):
    def test_finds_rust_extension_in_install_root_when_missing_from_version_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'install'
            version = root / '.planet-helper-versions' / 'v1.4.33-abc'
            root.mkdir()
            version.mkdir(parents=True)
            ext = root / '_planet_ocr.so'
            ext.write_bytes(b'fake')
            with patch.dict(os.environ, {'PLANET_HELPER_INSTALL_ROOT': str(root)}, clear=False):
                found = list(native_ocr._extension_candidates(version))
            self.assertEqual(found, [ext])


class TwoTierOcrTests(unittest.TestCase):
    def test_fast_model_short_circuits_medium_when_both_variants_strictly_agree(self):
        medium_calls = []
        fast_calls = []
        total = tracker.EXP_TABLE[170]
        exp = total // 2  # exactly 50.00%

        def medium(rgb):
            medium_calls.append(1)
            return SimpleNamespace(txts=(f'{exp} [50.00%]',), scores=(.99,))

        def fast(rgb):
            fast_calls.append(1)
            return SimpleNamespace(txts=(f'{exp} [50.00%]',), scores=(.96,))

        reader = tracker.LineReader(tracker.TwoTierOcr(medium, fast))
        image = Image.new('RGB', (80, 18), (20, 40, 20))
        value, score, raw, reused = reader.read(image, 10.0, force=True, level=170)
        self.assertEqual(value, (exp, 50.0))
        self.assertGreaterEqual(score, .92)
        self.assertFalse(reused)
        self.assertEqual(len(fast_calls), 2)
        self.assertEqual(len(medium_calls), 0)

    def test_fast_model_falls_back_to_medium_when_unsure(self):
        medium_calls = []
        total = tracker.EXP_TABLE[170]
        exp = total // 2

        def medium(rgb):
            medium_calls.append(1)
            return SimpleNamespace(txts=(f'{exp} [50.00%]',), scores=(.98,))

        def fast(rgb):
            return SimpleNamespace(txts=('garbage',), scores=(.40,))

        reader = tracker.LineReader(tracker.TwoTierOcr(medium, fast))
        image = Image.new('RGB', (80, 18), (20, 40, 20))
        value, score, _, _ = reader.read(image, 10.0, force=True, level=170)
        self.assertEqual(value, (exp, 50.0))
        self.assertGreaterEqual(len(medium_calls), 2)


class SharedDetectorTests(unittest.TestCase):
    def test_auto_hud_reuses_shared_map_name_detector_when_available(self):
        import map_name
        locator = auto_hud.HudLocator(engine=None)
        with patch.object(map_name, 'available', return_value=True):
            detector = locator._detector()
        self.assertIs(detector, map_name.detect_boxes)


class MacRawCaptureTests(unittest.TestCase):
    def test_cgimage_to_pil_decodes_bgra32_without_png(self):
        # 2x2 BGRX buffer with stride 12 (4 bytes padding per row)
        row0 = bytes([10, 20, 30, 255, 40, 50, 60, 255, 0, 0, 0, 0])
        row1 = bytes([70, 80, 90, 255, 100, 110, 120, 255, 0, 0, 0, 0])
        raw = row0 + row1
        quartz = SimpleNamespace(
            CGImageGetWidth=lambda _: 2,
            CGImageGetHeight=lambda _: 2,
            CGImageGetBitsPerPixel=lambda _: 32,
            CGImageGetBitsPerComponent=lambda _: 8,
            CGImageGetBytesPerRow=lambda _: 12,
            CGImageGetBitmapInfo=lambda _: 0x2000 | 2,  # 32Little | PremultipliedFirst (BGRA)
            CGImageGetDataProvider=lambda _: object(),
            CGDataProviderCopyData=lambda _: raw,
        )
        image = window_capture.cgimage_to_pil(object(), quartz, Image)
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (2, 2))
        self.assertEqual(image.getpixel((0, 0)), (30, 20, 10))
        self.assertEqual(image.getpixel((1, 0)), (60, 50, 40))
        self.assertEqual(image.getpixel((0, 1)), (90, 80, 70))
        self.assertEqual(image.getpixel((1, 1)), (120, 110, 100))

    def test_cgimage_to_pil_returns_none_for_unsupported_format(self):
        quartz = SimpleNamespace(
            CGImageGetWidth=lambda _: 2,
            CGImageGetHeight=lambda _: 2,
            CGImageGetBitsPerPixel=lambda _: 16,
            CGImageGetBitsPerComponent=lambda _: 5,
            CGImageGetBytesPerRow=lambda _: 4,
            CGImageGetBitmapInfo=lambda _: 0,
            CGImageGetDataProvider=lambda _: object(),
            CGDataProviderCopyData=lambda _: b'\x00' * 8,
        )
        self.assertIsNone(window_capture.cgimage_to_pil(object(), quartz, Image))


class HistoryCacheAndFilterTests(unittest.TestCase):
    def test_collect_uses_cache_and_invalidates_when_record_changes(self):
        from test_records_features import write_record
        with tempfile.TemporaryDirectory() as base:
            sid = write_record(base, character='본캐', gain=5000)
            first = record_history.collect(base)
            self.assertEqual(len(first), 1)
            self.assertTrue((Path(base) / 'records' / record_history.CACHE_FILE).is_file())
            with patch.object(record_history, 'summarize', side_effect=AssertionError('should hit cache')):
                second = record_history.collect(base)
            self.assertEqual(second[0]['session_id'], sid)
            self.assertEqual(second[0]['gained'], first[0]['gained'])
            # Touch the summary JSON to change its size/mtime -> cache invalidates cleanly.
            json_path = Path(base) / 'records' / f'{sid}.json'
            data = json.loads(json_path.read_text(encoding='utf-8'))
            data['character_assigned'] = '부캐'
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            third = record_history.collect(base)
            self.assertEqual(third[0]['character_name'], '부캐')

    def test_filter_rows_and_spot_meso_ranking(self):
        now = datetime(2026, 11, 27, 20, 0)
        rows = [
            dict(session_id='1', started=datetime(2026, 11, 27, 19, 0), level=170, active=1800, idle=0,
                 character_name='본캐', gained=180000, per_minute=6000, rate_gain=180000,
                 rate_seconds=1800, blocked=False, memo='사자왕의 성 · 낮은 성벽2', meso=500_000),
            dict(session_id='2', started=datetime(2026, 11, 23, 19, 0), level=169, active=1800, idle=0,
                 character_name='본캐', gained=120000, per_minute=4000, rate_gain=120000,
                 rate_seconds=1800, blocked=False, memo='사자왕의성 · 낮은 성벽2', meso=700_000),
            dict(session_id='3', started=datetime(2026, 10, 1, 19, 0), level=120, active=600, idle=0,
                 character_name='부캐', gained=60000, per_minute=6000, rate_gain=60000,
                 rate_seconds=600, blocked=False, memo='미니던전', meso=None),
        ]
        self.assertEqual([r['session_id'] for r in record_history.filter_rows(rows, period='today', now=now)], ['1'])
        self.assertEqual([r['session_id'] for r in record_history.filter_rows(rows, period='7d', now=now)], ['1', '2'])
        self.assertEqual([r['session_id'] for r in record_history.filter_rows(rows, query='사자왕의성', now=now)], ['1', '2'])
        self.assertEqual([r['session_id'] for r in record_history.filter_rows(rows, query='lv.120', now=now)], ['3'])
        detailed = record_history.by_memo_detailed(rows)
        spot_map = {entry[0]: entry for entry in detailed}
        spot = spot_map['사자왕의 성 · 낮은 성벽2']
        # Total meso = 1,200,000 over 3600s -> 1,200,000/hr across 2 records
        self.assertAlmostEqual(spot[4], 1_200_000.0)
        self.assertEqual(spot[5], 2)
        self.assertIsNone(spot_map['미니던전'][4])


class JevControlsAndUpdatePruningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_arbiter_gates_full_screen_separately_and_reports_summary(self):
        full_allowed = [False]
        arbiter = jev.Arbiter(
            lambda: True,
            cooldown=0.0,
            runner=lambda work: work(),
            full_screen_enabled=lambda: full_allowed[0],
        )
        self.assertFalse(arbiter.submit('fs', lambda: '사망', full_screen=True, label='멈춘 이유'))
        self.assertEqual(arbiter.calls, 0)
        self.assertTrue(arbiter.submit('crop', lambda: '본캐', full_screen=False, label='닉네임'))
        self.assertEqual(arbiter.calls, 1)
        self.assertIn('숫자·일부 영역만', arbiter.summary_text())
        self.assertIn('최근 닉네임: 본캐', arbiter.summary_text())
        full_allowed[0] = True
        self.assertTrue(arbiter.submit('fs', lambda: '사망', full_screen=True, label='멈춘 이유'))
        self.assertEqual(arbiter.calls, 2)
        self.assertIn('전체 허용', arbiter.summary_text())
        self.assertIn('최근 멈춘 이유: 사망', arbiter.summary_text())

    def test_controller_persists_jev_full_screen_and_meso_idle_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tracker, 'BASE', Path(tmp)):
                c = Controller(self.app, start_worker=False, restore=False)
                c.timer.stop()
                try:
                    self.assertTrue(c.jev_full_screen)
                    c.set_jev_full_screen(False)
                    self.assertFalse(tracker.load_settings(Path(tmp) / 'settings.json')[0]['jev_full_screen'])
                    self.assertEqual(c.meso_scan_interval(True), 1.0)
                    self.assertEqual(c.meso_scan_interval(False), c.MESO_IDLE_SCAN_INTERVAL)
                    c.meso_visible = True
                    self.assertEqual(c.meso_scan_interval(False), 2.0)
                    # Build history dialog with new filter widgets
                    dialog = record_history.build_dialog(c)
                    dialog.close()
                finally:
                    c.key_repeat.shutdown()
                    c.start_captures.close()
                    c._shutdown_probes()
                    c.window.hide()
                    c.window.deleteLater()
                    self.app.processEvents()

    def test_linux_updates_prunes_only_stale_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            versions = root / linux_updates.VERSIONS
            versions.mkdir()
            for name in ('v1.4.30-aaa', 'v1.4.31-bbb', 'v1.4.32-ccc', 'v1.4.33-ddd', 'custom-dir'):
                (versions / name).mkdir()
            (root / linux_updates.STATE).write_text(
                json.dumps({'current': 'v1.4.33-ddd', 'previous': 'v1.4.32-ccc'}), encoding='utf-8')
            removed = linux_updates.prune_old_versions(root)
            self.assertEqual(removed, 2)
            remaining = sorted(p.name for p in versions.iterdir())
            self.assertEqual(remaining, ['custom-dir', 'v1.4.32-ccc', 'v1.4.33-ddd'])

    def test_frame_ring_stores_compressed_jpeg_bytes_and_decodes_on_demand(self):
        import jev_assist
        ring = jev_assist.FrameRing()
        frame = Image.new('RGB', (1920, 1080), (40, 80, 120))
        ring.remember(0.0, frame)
        self.assertEqual(len(ring.frames), 1)
        stored = ring.frames[0][1]
        self.assertIsInstance(stored, bytes)
        self.assertLess(len(stored), 25_000)  # vs ~1.5 MB uncompressed RGB
        decoded = ring.latest()
        self.assertEqual(decoded.size, (960, 540))
        self.assertEqual(decoded.mode, 'RGB')

    def test_boundary_exp_binary_search_matches_exact_interpolation(self):
        session = tracker.Session(level=170)
        session.started = 0.0
        session.first = (1_000_000, 0.23)
        for i in range(0, 300):
            session.samples.append((float(i), 1_000_000 + i * 1000))
        exp_mid, gap_mid = session._boundary_exp(150.5)
        self.assertAlmostEqual(exp_mid, 1_150_500.0)
        self.assertAlmostEqual(gap_mid, 1.0)
        exp_exact, gap_exact = session._boundary_exp(200.0)
        self.assertAlmostEqual(exp_exact, 1_200_000.0)
        self.assertEqual(gap_exact, 0.0)
        self.assertEqual(session._boundary_exp(500.0), (None, None))

    def test_save_observation_throttles_summary_writes_while_appending_csv_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tracker, 'BASE', Path(tmp)):
                session = tracker.Session(level=170)
                writer = tracker.App.__new__(tracker.App)
                writer.session = session
                with patch.object(writer, 'save_summary', wraps=writer.save_summary) as flush:
                    for t, exp in ((0.0, 219671043), (1.0, 219672043), (2.0, 219673043),
                                   (3.0, 219674043), (4.0, 219675043), (8.0, 219676043)):
                        session.accept((exp, 51.32), t, corroborated=True, baseline_verified=True)
                        with patch.object(tracker.time, 'monotonic', return_value=t):
                            writer.save_observation()
                # Immediate on baseline (t=0), immediate on hunt-start (t=1), throttled at t=2..4, flushed at t=8.
                self.assertEqual(flush.call_count, 3)
                csv_lines = (Path(tmp) / 'records' / f'{session.session_id}.csv').read_text(encoding='utf-8-sig').splitlines()
                self.assertEqual(len(csv_lines), 1 + len(session.rows))

    def test_linux_capture_caches_visible_window_to_skip_tree_scan(self):
        if not __import__('sys').platform.startswith('linux'):
            self.skipTest('Linux-only X11 test')
        from Xlib import X
        from linux_capture import WindowCapture
        calls = {'list': 0, 'inspect': 0}

        class FakeWin:
            def __init__(self, wid): self.id = wid
            def get_wm_class(self):
                calls['inspect'] += 1
                return ('msw.exe', 'msw.exe')
            def get_full_property(self, *a): return SimpleNamespace(value=[])
            def get_attributes(self): return SimpleNamespace(map_state=X.IsViewable)
            def get_geometry(self): return SimpleNamespace(width=1280, height=720)

        w1 = FakeWin(42)
        cap = WindowCapture.__new__(WindowCapture)
        cap.state_atom, cap.hidden_atom = 'STATE', 'HIDDEN'
        cap.client_list_atom = 'CLIENT_LIST'
        cap._cached_wid = None
        cap.display = SimpleNamespace(
            intern_atom=lambda name: name,
            create_resource_object=lambda kind, wid: w1,
        )

        def get_list(*a):
            calls['list'] += 1
            return SimpleNamespace(value=[1, 2, 3, 42])

        cap.root = SimpleNamespace(
            get_full_property=get_list,
            translate_coords=lambda w, x, y: SimpleNamespace(x=0, y=0),
        )
        # First call scans _NET_CLIENT_LIST and caches wid=42.
        self.assertEqual(cap.game()[0].id, 42)
        self.assertEqual(calls['list'], 1)
        # Subsequent calls hit O(1) cache without touching _NET_CLIENT_LIST.
        for _ in range(5):
            self.assertEqual(cap.game()[0].id, 42)
        self.assertEqual(calls['list'], 1)

    def test_macos_window_capture_caches_filter_and_requests_bgra(self):
        import window_capture
        allocs = {'filter': 0, 'cfg': 0, 'pixel_fmt': None}

        class FakeFilter:
            def initWithDesktopIndependentWindow_(self, w):
                allocs['filter'] += 1
                return self
            def contentRect(self):
                return SimpleNamespace(size=SimpleNamespace(width=1280, height=720))
            def pointPixelScale(self):
                return 2.0

        class FakeConfig:
            def init(self):
                allocs['cfg'] += 1
                return self
            def setShowsCursor_(self, v): pass
            def setCapturesAudio_(self, v): pass
            def setIgnoreShadowsSingleWindow_(self, v): pass
            def setShouldBeOpaque_(self, v): pass
            def setPixelFormat_(self, v): allocs['pixel_fmt'] = v
            def setWidth_(self, v): self.w = v
            def setHeight_(self, v): self.h = v

        SCContentFilter = SimpleNamespace(alloc=lambda: FakeFilter())
        SCStreamConfiguration = SimpleNamespace(alloc=lambda: FakeConfig())
        cap = window_capture.WindowCapture()
        win = SimpleNamespace(windowID=lambda: 77)
        bounds = (0.0, 0.0, 1280.0, 720.0)
        res1 = cap._filter_and_config(win, bounds, SCContentFilter, SCStreamConfiguration)
        res2 = cap._filter_and_config(win, bounds, SCContentFilter, SCStreamConfiguration)
        self.assertIs(res1[0], res2[0])
        self.assertIs(res1[4], res2[4])
        self.assertEqual(allocs['filter'], 1)
        self.assertEqual(allocs['cfg'], 1)
        self.assertEqual(allocs['pixel_fmt'], window_capture.WindowCapture.PIXEL_FORMAT_BGRA)
        # Moving/resizing invalidates the cached filter.
        cap._filter_and_config(win, (10.0, 0.0, 1280.0, 720.0), SCContentFilter, SCStreamConfiguration)
        self.assertEqual(allocs['filter'], 2)

    def test_retina_high_dpi_exp_crop_avoids_4x_over_upscaling(self):
        sizes = []
        class FakeOcr:
            def __call__(self, rgb):
                sizes.append(rgb.shape[:2])
                return SimpleNamespace(txts=('219671043 [51.32%]',), scores=(0.99,))
        reader = tracker.LineReader(FakeOcr())
        # Retina 2x crop (height=56): should stay at 1x/2x (height <= 120), NOT 56*4=224+40=264!
        retina = Image.new('RGB', (400, 56), (20, 20, 20))
        reader.read(retina, 1.0, force=True, level=170)
        self.assertTrue(sizes)
        self.assertLessEqual(max(h for h, _ in sizes), 120)

    def test_key_repeat_bridge_backs_off_polling_when_permissions_are_ready(self):
        import key_repeat
        with tempfile.TemporaryDirectory() as tmp:
            bridge = key_repeat.KeyRepeatBridge(tmp)
            emitted = []
            bridge.changed.connect(emitted.append)
            steady_msg = json.dumps({
                'type': 'status', 'enabled': False, 'active': False,
                'accessibility': True, 'inputMonitoring': True,
                'legacyRunning': False, 'status': '연타 꺼짐', 'error': '',
            }).encode('utf-8') + b'\n'
            with patch.object(bridge.process, 'readAllStandardOutput', return_value=steady_msg):
                bridge._read()
                bridge._read()  # identical state should not re-emit
            self.assertEqual(len(emitted), 1)
            self.assertEqual(bridge.poll_timer.interval(), 10000)

    def test_character_hud_caches_unchanged_lines_and_short_circuits_after_two_agreements(self):
        import character_hud
        character_hud._LINE_CACHE.clear()
        line = Image.new('RGB', (120, 20), (20, 30, 40))
        with patch('map_name.recognize_lines', return_value=[('곰곰스렁', 0.99)]) as rec:
            self.assertEqual(character_hud._read_line(line, job=False), '곰곰스렁')
            # Early short-circuit after 2 agreeing variants instead of 3!
            self.assertEqual(rec.call_count, 2)
            # Identical pixels hit cache with 0 OCR calls!
            self.assertEqual(character_hud._read_line(line, job=False), '곰곰스렁')
            self.assertEqual(rec.call_count, 2)

    def test_exp_buffs_locks_matched_scale_for_resolution(self):
        import exp_buffs
        with tempfile.TemporaryDirectory() as tmp:
            icon = Image.new('RGB', (36, 36), (30, 30, 30))
            for y in range(6, 20):
                for x in range(6, 30):
                    icon.putpixel((x, y), (240, 200, 40))
            src = Path(tmp) / 'src.png'
            icon.save(src)
            exp_buffs.register(tmp, 'coupon', src)
            frame = Image.new('RGB', (1200, 750), (20, 20, 20))
            frame.paste(icon, (900, 80))
            det = exp_buffs.Detector()
            self.assertEqual(det.read(frame, tmp)['coupon'], 'active')
            self.assertIn(('coupon', (1200, 750)), det.locked_scales)
            # When buff disappears at the same resolution, only 3 scales around locked scale are checked.
            empty = Image.new('RGB', (1200, 750), (20, 20, 20))
            with patch('cv2.matchTemplate', wraps=__import__('cv2').matchTemplate) as mt:
                self.assertEqual(det.read(empty, tmp)['coupon'], 'unknown')
                self.assertLessEqual(mt.call_count, 3)


if __name__ == '__main__':
    unittest.main()

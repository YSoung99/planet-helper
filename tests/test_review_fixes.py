import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
import tracker
from rates import estimate
from temporal_ocr import save_rejected, valid, verify, pixel_hash, usable_image


class ReviewFixTests(unittest.TestCase):
    def test_cache_restores_successful_provenance_after_failed_image(self):
        replies = iter(['1000[10.00%]', '1000[10.00%]', '?', '?', '?'])
        reader = tracker.LineReader(lambda image: SimpleNamespace(txts=(next(replies),), scores=(.99,)))
        good, bad = Image.new('RGB', (100, 20), 'black'), Image.new('RGB', (100, 20), 'white')
        reader.read(good, 1)
        expected = list(reader.last_readings)
        reader.read(bad, 2)
        self.assertEqual(reader.last_readings, ['?', '?', '?'])
        result = reader.read(good, 3)
        self.assertEqual(result[0], (1000, 10)); self.assertTrue(result[-1])
        self.assertEqual(reader.last_readings, expected)
        reader.last_readings.append('external mutation')
        reader.read(good, 4)
        self.assertEqual(reader.last_readings, expected)

    def test_uniform_chromatic_images_have_no_spatial_structure(self):
        for color in ('#405060', '#00ff00', '#ff0000', '#0000ff'):
            image = Image.new('RGB', (100, 20), color)
            self.assertFalse(usable_image(image))
            image.putpixel((3, 3), (255, 255, 255))
            self.assertTrue(usable_image(image))

    def frames(self, reason=None):
        return [dict(image=Image.new('RGB', (50, 10), (i*30, 0, 0)), rejection_reason=reason)
                for i in range(3)]

    def test_metadata_and_image_failures_leave_no_partial_groups(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            for target in ('metadata', 'image'):
                obj, attr = (Path, 'write_text') if target == 'metadata' else (Image.Image, 'save')
                with patch.object(obj, attr, side_effect=OSError('injected disk failure')):
                    for _ in range(5):
                        with self.assertRaises(OSError):
                            save_rejected(folder, self.frames(), {'reason': 'inconsistent'}, limit=2)
                self.assertEqual(list(folder.rglob('*.png')), [])
                self.assertEqual(list(folder.rglob('*.json')), [])
                self.assertEqual(list(folder.rglob('.pending-*')), [])
            save_rejected(folder, self.frames(), {'reason': '복구'}, limit=2)
            manifest = next(folder.rglob('evidence.json'))
            self.assertEqual(json.loads(manifest.read_text(encoding='utf-8'))['reason'], '복구')
            self.assertEqual(len(list(manifest.parent.glob('*.png'))), 3)

    def test_retention_deduplicates_obstructions_without_evicting_mismatches(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder/'123-0.png').write_bytes(b'orphan')
            (folder/'unrelated.png').write_bytes(b'keep')
            for _ in range(4):
                save_rejected(folder, self.frames(), {'reason': 'inconsistent'}, limit=2)
            mismatch = sorted((folder/'mismatch').iterdir())
            self.assertEqual(len(mismatch), 2)
            for _ in range(40):
                save_rejected(folder, self.frames('blank'), {'reason': 'insufficient'}, limit=2)
            self.assertEqual(sorted((folder/'mismatch').iterdir()), mismatch)
            groups = list((folder/'unobservable').iterdir())
            self.assertEqual(len(groups), 1)
            self.assertEqual(json.loads((groups[0]/'evidence.json').read_text())['repetitions'], 40)
            self.assertFalse((folder/'123-0.png').exists())
            self.assertTrue((folder/'unrelated.png').exists())
            for i in range(5):
                frames = self.frames('covered'); frames[0]['image'].putpixel((0, 0), (i, i, i))
                save_rejected(folder, frames, {}, limit=2)
            self.assertEqual(len(list((folder/'unobservable').iterdir())), 2)

    def test_stopped_average_retains_observed_endpoints_and_gap(self):
        points = [(0, 1000), (60, 1600)]
        self.assertIsNone(estimate(points, (0, 1000), 200)['cumulative'])
        frozen = estimate(points, (0, 1000), 10000, ended=200)
        self.assertEqual(frozen['cumulative'], 600)
        self.assertEqual(frozen['asof'], 60); self.assertEqual(frozen['age'], 140)
        self.assertEqual(frozen['recent_status'], 'gap')

    def test_shared_tolerance_matches_session_for_estimated_and_explicit_total(self):
        s = tracker.Session()
        s.accept((200100, 20), 0); s.accept((200100, 20), 1)
        frame = dict(value=(600000, 60), score=.99)
        self.assertTrue(valid(frame, s.total, level=s.level))
        s.accept(frame['value'], 2)
        self.assertEqual(s.last, frame['value'])
        self.assertFalse(valid(frame, s.total, level=170))

    def test_temporal_hashes_identify_same_pixels_without_claiming_independence(self):
        def frame(stamp):
            return dict(image=Image.new('RGB', (50, 10), '#405060'), value=(1000, 10),
                        score=.99, raw='1000[10%]', observed_at=stamp)
        remaining = iter([frame(2), frame(3)])
        selected, evidence, frames = verify(frame(1), lambda force: next(remaining), 10000, None,
                                            lambda: False, lambda delay: False)
        self.assertEqual(selected['observed_at'], 3)
        self.assertEqual(len(set(f['pixel_hash'] for f in evidence['frames'])), 1)
        self.assertEqual(evidence['frames'][0]['pixel_hash'], pixel_hash(frames[0]['image']))

    def session_with_false_high(self):
        s = tracker.Session(level=170)
        for t, v in [(0, (4000000, .93)), (1, (4000000, .93)), (2, (4100000, .96)),
                     (61, (4100000, .96)), (62, (4105000, .96))]:
            s.accept(v, t, corroborated=True)
        return s

    def test_repeated_corroborated_lower_conflict_stops_without_rewriting_history(self):
        s = self.session_with_false_high(); original = list(s.rows)
        for stamp in (63, 64, 65):
            s.accept((4100000, .96), stamp, corroborated=True)
        self.assertTrue(s.blocked); self.assertIn('오인식/실제 감소', s.stop_reason)
        self.assertEqual(s.rows, original); self.assertEqual(s.stats(65)[0], 105000)

    def test_transient_uncorroborated_or_old_lower_value_does_not_stop(self):
        s = self.session_with_false_high()
        for stamp in (63, 64, 65): s.accept((4100000, .96), stamp)
        self.assertFalse(s.blocked)
        s.accept((4100000, .96), 66, corroborated=True)
        s.accept((4100000, .96), 80, corroborated=True)
        self.assertEqual(s.small_decrease_count, 1)
        s.accept((4105000, .96), 81)
        self.assertEqual(s.small_decrease_count, 0)
        s.accept((4100000, .96), 82, corroborated=True)
        self.assertFalse(s.blocked)

    def test_conflict_preserves_prior_accepted_pixels_separate_from_rejected_bursts(self):
        from temporal_ocr import remember_change, save_conflict
        with tempfile.TemporaryDirectory() as name:
            folder=Path(name)
            high=self.frames(); high[0]['image'].info['full_game_image']=Image.new('RGB',(100,100))
            previous=remember_change(high,{'reason':'stable','value':[4105000,.96]},'one',1,(4105000,.96))
            self.assertEqual(previous['frames'][0]['image'].info,{})
            high[0]['image'].putpixel((0,0),(255,255,255))
            self.assertNotEqual(pixel_hash(high[0]['image']),pixel_hash(previous['frames'][0]['image']))
            low=self.frames()
            save_conflict(folder,previous,low,{'reason':'stable'},(4105000,.96),'one',1)
            group=next((folder/'conflict').iterdir())
            saved=json.loads((group/'evidence.json').read_text())
            self.assertEqual(saved['previous_frame_count'],3)
            self.assertEqual(saved['current_frame_count'],3)
            self.assertEqual(len(list(group.glob('*.png'))),6)
            for _ in range(5):save_rejected(folder,self.frames(),{},limit=2)
            self.assertTrue(group.exists())
            save_conflict(folder,previous,low,{},(4105000,.96),'new-session',2)
            group=sorted((folder/'conflict').iterdir())[-1]
            self.assertTrue(json.loads((group/'evidence.json').read_text())['previous_change_missing'])
            self.assertEqual(len(list(group.glob('*.png'))),3)

    def test_worker_writes_actual_lower_capture_with_prior_accepted_group(self):
        import queue
        from temporal_ocr import remember_change
        class Stop:
            done=False
            def is_set(self):return self.done
            def wait(self,delay):
                if delay==.15:return False
                self.done=True;return True
        s=self.session_with_false_high()
        image=Image.new('RGB',(100,20),'#444444');image.putpixel((1,1),(255,255,255))
        old=remember_change([{'image':image}],{'selected':0},s.session_id,1,s.last)
        fake=SimpleNamespace(running=True,generation=1,region={'left':0,'top':0,'width':100,'height':20},
            engine=lambda img:SimpleNamespace(txts=('4100000[0.96%]',),scores=(.99,)),
            stop=Stop(),events=queue.Queue(),scan_seconds=0,target_seconds=1,session=s,last_accepted_evidence=old)
        process=SimpleNamespace(nice=lambda value:None,cpu_times=lambda:(0,0))
        with tempfile.TemporaryDirectory() as name,patch.object(tracker,'BASE',Path(name)), \
             patch('psutil.Process',return_value=process),patch('psutil.cpu_percent',return_value=0), \
             patch.object(tracker,'capture_image',return_value=image):
            tracker.App.worker(fake)
            event=fake.events.get_nowait()
            self.assertEqual(event[1],(4100000,.96))
            self.assertIsNotNone(event[6]['temporal']['selected'])
            manifest=next((Path(name)/'records'/'verification-frames'/'conflict').rglob('evidence.json'))
            saved=json.loads(manifest.read_text())
            self.assertEqual(saved['previous_frame_count'],1)
            self.assertEqual(saved['current_frame_count'],3)

    def test_conflict_retention_cannot_shrink_legacy_diagnostic_limit(self):
        from temporal_ocr import save_conflict
        with tempfile.TemporaryDirectory() as name:
            folder=Path(name)
            for index in range(35):
                prefix=str(1000+index)
                (folder/(prefix+'.json')).write_text('{}')
                (folder/(prefix+'-0.png')).write_bytes(b'legacy')
            save_conflict(folder,None,self.frames(),{},(1000,10),'one',1)
            self.assertEqual(len(list(folder.glob('*.json'))),30)
            self.assertEqual(len(list(folder.glob('*.png'))),30)
            self.assertTrue((folder/'1005.json').exists())

    def test_percentage_conflict_interrupts_small_decrease_sequence(self):
        s=self.session_with_false_high()
        s.accept((4100000,.96),63,corroborated=True)
        s.accept((4105000,.95),64,corroborated=True)
        self.assertEqual(s.small_decrease_count,0)
        self.assertFalse(s.blocked)

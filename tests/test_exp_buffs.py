import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import exp_buffs
from record_history import buff_cell


class BuffTests(unittest.TestCase):
    def test_cached_frame_and_replaced_template(self):
        from cv2 import matchTemplate

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            frame = Image.new('RGB', (1050, 750))
            icon = Image.new('RGB', (37, 37), (37, 61, 79))
            draw = ImageDraw.Draw(icon)
            draw.ellipse((4, 4, 28, 20), fill=(242, 40, 13))
            draw.line((7, 21, 27, 6), fill=(51, 234, 157), width=4)
            source = root / 'icon.png'
            icon.save(source)
            frame.paste(icon, (982, 88))
            detector = exp_buffs.Detector()
            self.assertEqual(detector.read(frame, root)['coupon'], 'unregistered')
            exp_buffs.register(root, 'coupon', source)
            with patch('cv2.matchTemplate', wraps=matchTemplate) as match:
                self.assertEqual(detector.read(frame, root)['coupon'], 'active')
                self.assertEqual(detector.read(frame.copy(), root)['coupon'], 'active')
                self.assertEqual(match.call_count, 1)
            exp_buffs.register(root, 'holy_symbol', source)
            self.assertEqual(detector.read(frame, root), dict.fromkeys(exp_buffs.LABELS, 'unknown'))
            self.assertEqual(detector.read(Image.new('RGB', frame.size), root),
                             dict.fromkeys(exp_buffs.LABELS, 'unknown'))

    def test_confirmation_and_record_do_not_bridge_missing_frames(self):
        confirmation = exp_buffs.Confirmation()
        intervals = {}
        active = {'coupon': 'active', 'holy_symbol': 'unknown'}
        blank = dict.fromkeys(exp_buffs.LABELS, 'unknown')
        for stamp, readings in [(10, active), (12, active), (14, active),
                                (16, blank), (18, active), (20, active), (40, active)]:
            start = confirmation.observe((1, 1), stamp, readings)
            if start is not None:
                exp_buffs.record(intervals, confirmation.current, start - 10, stamp - 10)
        self.assertEqual(intervals, {'coupon': [[0, 4], [8, 10]]})
        self.assertNotIn('사용 중', confirmation.text(1, 40))
        self.assertEqual(buff_cell(intervals), ('경쿠', '경쿠 0:00–0:04, 0:08–0:10'))

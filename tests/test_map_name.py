import tempfile
import unittest

import map_name
import record_history


class MapNameTests(unittest.TestCase):
    def test_uses_first_two_korean_lines_and_skips_header(self):
        lines = [(30, '낮은 성벽3', .9), (5, 'MINI MAP', .9), (5, 'WORLD', .9), (18, '사자왕의  성', .95)]
        self.assertEqual(map_name.lines_to_name(lines), '사자왕의 성 · 낮은 성벽3')

    def test_low_confidence_gives_no_name(self):
        self.assertIsNone(map_name.lines_to_name([(1, '일본', .97), (2, '주차장', .3)]))
        self.assertIsNone(map_name.lines_to_name([(1, 'MINI MAP', .9)]))

    def test_correction_is_learned_and_applied_to_matching_records(self):
        with tempfile.TemporaryDirectory() as base:
            for session in ('a', 'b', 'c'):
                map_name.remember_auto(session, '사자왕의 성 · 낮은 성벽위', base)
                record_history.save_memo(session, '사자왕의 성 · 낮은 성벽위', base)
            record_history.save_memo('c', '내가 쓴 메모', base)
            self.assertTrue(map_name.learn_correction('a', '사자왕의 성 · 낮은 성벽3', base))
            memos = record_history.load_memos(base)
            self.assertEqual(memos['b'], '사자왕의 성 · 낮은 성벽3')
            self.assertEqual(memos['c'], '내가 쓴 메모')
            self.assertEqual(map_name.resolve('사자왕의 성 · 낮은 성벽위', base), '사자왕의 성 · 낮은 성벽3')


if __name__ == '__main__':
    unittest.main()

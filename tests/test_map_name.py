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

    def test_jev_is_asked_only_when_ocr_is_not_a_confirmed_name(self):
        with tempfile.TemporaryDirectory() as base:
            asked = []
            ask = lambda picture, names: asked.append(names) or names[0]
            self.assertIsNone(map_name.choose('사자왕의 성 · 낮은 성벽스', None, base, ask))  # nothing confirmed yet
            record_history.save_memo('typed', '사자왕의 성 · 낮은 성벽2', base)
            self.assertIsNone(map_name.choose('사자왕의 성 · 낮은 성벽2', None, base, ask))
            self.assertEqual(asked, [])
            self.assertEqual(map_name.choose('사자왕의 성 · 낮은 성벽스', None, base, ask), '사자왕의 성 · 낮은 성벽2')
            map_name.learn_alias('사자왕의 성 · 낮은 성벽스', '사자왕의 성 · 낮은 성벽2', base)
            self.assertIsNone(map_name.choose('사자왕의 성 · 낮은 성벽스', None, base, ask))
            self.assertEqual(len(asked), 1)

    def test_jev_answer_needs_high_probability_and_a_listed_name(self):
        names = ['사자왕의 성 · 낮은 성벽2', '사자왕의 성 · 높은 성벽1']
        reply = lambda probabilities: {'answers': [{'name': 'map', 'type': 'choice', 'probabilities': [
            {'value': value, 'probability': p} for value, p in probabilities.items()]}]}
        self.assertEqual(map_name.parse_pick(reply({'0': .02, '1': .97, 'none': .01}), names), names[1])
        self.assertIsNone(map_name.parse_pick(reply({'0': .6, '1': .3, 'none': .1}), names))
        self.assertIsNone(map_name.parse_pick(reply({'0': 0, '1': .01, 'none': .99}), names))
        self.assertIsNone(map_name.parse_pick({'error': 'x'}, names))

    def test_key_store_uses_stdin_on_linux_and_never_argv(self):
        from unittest.mock import patch
        import subprocess
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs.get('input')))
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(map_name.sys, 'platform', 'linux'), patch.object(map_name.subprocess, 'run', run):
            map_name.store_key('  secret-value  ')
        command, data = calls[0]
        self.assertEqual(data, 'secret-value')
        self.assertNotIn('secret-value', ' '.join(command))


if __name__ == '__main__':
    unittest.main()

import unittest
from unittest import mock

from PIL import Image

from hunt_activity import Activity, Observation, reward_types


class HuntActivityTests(unittest.TestCase):
    def test_only_high_confidence_gain_messages_count_as_rewards(self):
        lines = [('경험치를 얻었습니다 (+131500)', .98),
                 ('메소를 얻었습니다 (+7,478)', .96),
                 ('버프 보너스 경험치 (+2630)', .99),
                 ('기타 아이템을 얻었습니다 (벌쳐의 어깨장식)', .93)]
        self.assertEqual(reward_types(lines), {'경험치', '메소'})
        self.assertEqual(reward_types(lines[2:] + [(lines[0][0], .63)]), set())

    def test_dialog_overrides_previous_experience_and_rewards(self):
        activity = Activity()
        reward = Observation(frozenset({'경험치'}))
        activity.update(1, 2, reward)
        activity.update(1, 3, gained=True)
        self.assertIn('사냥 중 ·', activity.text(1, 3))
        activity.update(1, 4, Observation(reward.rewards, interrupted=True))
        self.assertIn('사냥 중단', activity.text(1, 4))
        activity.update(1, 6, reward)
        self.assertIn('증가 확인 대기', activity.text(1, 6))
        activity.update(1, 7, gained=True)
        self.assertIn('사냥 중 ·', activity.text(1, 7))

    def test_missing_and_old_observations_are_unconfirmed(self):
        activity = Activity()
        activity.update(1, 2, Observation(frozenset({'메소'})))
        self.assertIn('사냥 여부 확인 중', activity.text(1, 3))
        self.assertEqual(activity.text(1, 11), '사냥 신호 확인 중')
        self.assertEqual(activity.text(2, 3), '사냥 신호 확인 중')

    def test_identical_reward_pixels_reuse_previous_ocr(self):
        import hunt_activity
        frame = Image.new('RGB', (400, 300), (200, 200, 200))
        lines = [('경험치를 얻었습니다 (+100)', .97)]
        cache = {}
        with mock.patch('map_name.recognize_lines', return_value=lines) as recognize:
            first = hunt_activity.read(frame, cache)
            second = hunt_activity.read(frame.copy(), cache)
            self.assertEqual(recognize.call_count, 1)
            self.assertEqual(first, second)
            changed = frame.copy()
            changed.putpixel((390, 250), (0, 0, 0))
            hunt_activity.read(changed, cache)
            self.assertEqual(recognize.call_count, 2)
        with mock.patch('map_name.recognize_lines', return_value=lines) as recognize:
            hunt_activity.read(frame)
            hunt_activity.read(frame)
            self.assertEqual(recognize.call_count, 2)



class CharacterRecheckTests(unittest.TestCase):
    def test_confirmed_character_skips_ocr_until_recheck(self):
        from types import SimpleNamespace
        from qt_controller import SharedController
        state = SimpleNamespace(character_source=(1, 7), character_identity=('아란', '닉네임'),
                                character_recheck_at=100.0)
        needed = lambda *args: SharedController.character_read_needed(state, *args)
        self.assertFalse(needed(1, 7, 50.0))
        self.assertTrue(needed(1, 7, 100.0))
        self.assertTrue(needed(2, 7, 50.0))
        self.assertTrue(needed(1, 8, 50.0))
        state.character_identity = (None, '닉네임')
        self.assertTrue(needed(1, 7, 50.0))

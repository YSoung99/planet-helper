import unittest

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

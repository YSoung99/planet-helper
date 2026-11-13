import unittest
from temporal_ocr import decide,verify


def frame(exp,pct=None,score=.99,stamp=0):
    return dict(value=None if exp is None else (exp,exp/100 if pct is None else pct),score=score,
                observed_at=stamp,raw=str(exp))


class TemporalTests(unittest.TestCase):
    def test_fast_increase_does_not_require_identical_values(self):
        self.assertEqual(decide([frame(100),frame(110),frame(120)],10000),(1,'progression_middle'))
    def test_isolated_high_low_and_middle_misreads_do_not_increase_total(self):
        for values in ([900,100,100],[100,900,100],[100,100,900]):
            index,reason=decide([frame(x) for x in values],10000)
            self.assertEqual(values[index],100)
        self.assertEqual(decide([frame(100),frame(120),frame(110)],10000),(None,'inconsistent'))
    def test_digit_percentage_mismatch_and_low_confidence_cannot_vote(self):
        self.assertEqual(decide([frame(100),frame(110,pct=9),frame(120,score=.5)],10000),(None,'insufficient'))
    def test_stable_decrease_remains_visible_to_existing_death_checks(self):
        index,_=decide([frame(500),frame(400),frame(400)],10000)
        self.assertEqual(index,2)
    def test_cancelled_burst_never_reaches_ledger_and_followups_bypass_cache(self):
        calls=[]
        def capture(force):calls.append(force);return frame(100,stamp=len(calls))
        selected,evidence,_=verify(frame(100),capture,10000,None,lambda:False,lambda delay:False)
        self.assertEqual(calls,[True,True]);self.assertEqual(selected['observed_at'],2)
        selected,evidence,_=verify(frame(100),capture,10000,None,lambda:True,lambda delay:False)
        self.assertIsNone(selected);self.assertEqual(evidence['reason'],'cancelled')
    def test_unchanged_known_value_does_not_burst(self):
        selected,evidence,frames=verify(frame(100),lambda force:self.fail(),10000,(100,1.0),lambda:False,lambda delay:False)
        self.assertFalse(evidence['checked']);self.assertEqual(frames,[])

    def test_advancing_sequence_preserves_middle_capture_timestamp(self):
        remaining=iter([frame(110,stamp=11),frame(900,stamp=12)])
        selected,evidence,_=verify(frame(100,stamp=10),lambda force:next(remaining),10000,None,
                                   lambda:False,lambda delay:False)
        self.assertEqual(selected['value'],(110,1.1))
        self.assertEqual(selected['observed_at'],11)
        self.assertEqual(evidence['reason'],'progression_middle')


class BlankImageTests(unittest.TestCase):
    def test_black_white_and_tiny_crops_cannot_be_evidence(self):
        from PIL import Image
        from temporal_ocr import usable_image
        for color in ('black','white','#444444'):
            self.assertFalse(usable_image(Image.new('RGB',(100,20),color)))
        self.assertFalse(usable_image(Image.new('RGB',(10,2))))

import unittest
from types import SimpleNamespace
from PIL import Image
import tracker

class HudPunctuationTests(unittest.TestCase):
    def test_complete_labeled_percentage_may_omit_only_closing_decoration(self):
        for text in ('EXP 219671043 [51.32%', 'EXP. 219671043 (51.32％', 'exp 219671043 {51.32%'):
            with self.subTest(text=text):self.assertEqual(tracker.parse(text),(219671043,51.32))

    def test_mixed_closing_ornaments_and_existing_percent_variants_keep_digits(self):
        for text in ('EXP 218651376 [51.08)]','EXP 218651376 (51.08}]',
                     'EXP 218651376 [51.08×]','218651376 [51.08×]',
                     'EXP 218651376 [51.08X]', '218,651,376 [51,08%]]'):
            with self.subTest(text=text):self.assertEqual(tracker.parse(text),(218651376,51.08))

    def test_truncated_percentage_extra_digits_and_unsupported_text_stay_withheld(self):
        bad=('EXP 215476644 [50.3','EXP 215476644 [5','EXP 215476644 [50.34',
             '215476644 [50.34%','EXP 219610864 (51.31X','EXP 219610864 (51.31×',
             'EXP 215476644 50.34%','EXP 215476644 [50.34%garbage',
             'EXP 215476644 [50.34%!','EXP 215476644 [100.34%',
             'EXP 12345[1.2%]','EXP 12345[1.234%]','EXP 12345[1.23%]]]]')
        for text in bad:
            with self.subTest(text=text):self.assertIsNone(tracker.parse(text))

    def test_missing_opening_bracket_needs_the_level_total_to_agree(self):
        total=tracker.EXP_TABLE[122]
        # Real level-122 readings where OCR dropped only the "[".
        for text,value in (('EXP 3698597 11.19%]',(3698597,11.19)),('EXP 2968939 8.98%]',(2968939,8.98)),
                           ('EXP. 3754877 11.36％]',(3754877,11.36))):
            with self.subTest(text=text):
                self.assertEqual(tracker.parse(text,total),value)
                self.assertIsNone(tracker.parse(text))
        for text in ('EXP 2968939 18.98%]',   # "[" misread as "1"
                     'EXP 272009 18.23%]',     # dropped digits
                     'EXP 3698597 11.19%',     # no closing bracket either
                     '3698597 11.19%]',        # no EXP label
                     'EXP 369859711.19%]',     # no separating space
                     'EXP 3698597 11.19]',     # no percent sign
                     'EXP 3698597 11.19X]'):
            with self.subTest(text=text):self.assertIsNone(tracker.parse(text,total))

    def test_doubled_bracket_and_space_before_decimal_need_the_level_total(self):
        total=tracker.EXP_TABLE[122]
        # Real level-122 readings from the same hunt.
        for text,value in (('EXP. 16545649 [(50.04%]',(16545649,50.04)),
                           ('EXP 13749458 [41 .59%]',(13749458,41.59)),
                           ('EXP 13620818 [41 .20%]',(13620818,41.2))):
            with self.subTest(text=text):
                self.assertEqual(tracker.parse(text,total),value)
                self.assertIsNone(tracker.parse(text))
        # A dropped digit is well-formed, so the later EXP-table check rejects it.
        from temporal_ocr import consistent_value
        self.assertFalse(consistent_value(tracker.parse('EXP 16272759 [4.22%]',total),total,122))
        for text in ('EXP 16272759 [4 9.22%]',   # space between digits is not a decimal point
                     'EXP. 16545649 [(60.04%]',  # total disagrees
                     'EXP 123 45[1 .23%]'):
            with self.subTest(text=text):self.assertIsNone(tracker.parse(text,total))

    def test_line_reader_recovers_a_reading_without_opening_bracket(self):
        replies=iter(['EXP 3698597 11.19%]']*3)
        reader=tracker.LineReader(lambda img:SimpleNamespace(txts=(next(replies),),scores=(.94,)))
        result=reader.read(Image.new('RGB',(332,30)),1,force=True,level=122)
        self.assertEqual(result[0],(3698597,11.19))

    def test_spaces_cannot_join_separate_numeric_fragments(self):
        for text in ('EXP 123 45[1.23%]','EXP 12345[1 .23%]','EXP 12345[1.2 3%]'):
            with self.subTest(text=text):self.assertIsNone(tracker.parse(text))

    def test_original_labels_zero_and_delimited_fields_keep_working(self):
        for text in ('0[0.00%]',' EXP 0 [0.00%] ','EXP.0 [0,00%]]'):
            with self.subTest(text=text):self.assertEqual(tracker.parse(text),(0,0.0))

    def test_actual_line_reader_vote_uses_the_new_punctuation_parser(self):
        replies=iter(['EXP 219671043 [51.32%', 'EXP 219671043 [51.32%]'])
        reader=tracker.LineReader(lambda img:SimpleNamespace(txts=(next(replies),),scores=(.99,)))
        result=reader.read(Image.new('RGB',(332,30)),1,force=True)
        self.assertEqual(result[0],(219671043,51.32))
        self.assertEqual(len(reader.last_readings),2)

    def test_numeric_percentage_validation_remains_outside_format_parser(self):
        control=tracker.Session(level=170)
        good=tracker.parse('EXP 219671043 [51.32%')
        control.accept(good,1);control.accept(good,2)
        self.assertEqual(control.first,(219671043,51.32))
        self.assertEqual(len(control.rows),1)
        session=tracker.Session(level=170)
        wrong=tracker.parse('EXP 219671043 [71.32%')
        self.assertIsNotNone(wrong)
        session.accept(wrong,1);session.accept(wrong,2)
        self.assertIsNone(session.first)
        self.assertEqual(session.rows,[])

    def test_comma_groups_do_not_bypass_the_original_twelve_digit_limit(self):
        self.assertEqual(tracker.parse('123,456,789,012 [1.23%]'),(123456789012,1.23))
        for text in ('1,234,567,890,123 [1.23%]','218,65,1376 [51.08%]'):
            with self.subTest(text=text):self.assertIsNone(tracker.parse(text))

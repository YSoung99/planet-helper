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

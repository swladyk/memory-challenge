import unittest
import workflow as w
from test_workflow import H, Q, run


class CitationFallbackTests(unittest.TestCase):
    def test_off_by_default(self):
        rows, warnings = w.make_predictions(Q, H, [run(answer='The wedding moved to June.')])
        self.assertEqual(rows[0]['citations'], [])
        self.assertIn('q1: no final-answer citations', warnings)

    def test_supported_answer_gets_its_evidence_session(self):
        log = []
        rows, warnings = w.make_predictions(Q, H, [run(answer='The wedding moved to June.')], True, log)
        self.assertEqual(rows[0]['citations'], ['s1'])
        self.assertEqual(rows[0]['answer'], 'The wedding moved to June.')  # answer untouched
        self.assertEqual(log[0]['question_id'], 'q1')
        self.assertTrue(any('fallback' in x for x in warnings))

    def test_unsupported_answer_stays_uncited(self):
        log = []
        rows, _ = w.make_predictions(Q, H, [run(answer='In Lisbon, by the river.')], True, log)
        self.assertEqual(rows[0]['citations'], [])
        self.assertIn('left empty', log[0]['reason'])

    def test_labelled_answers_are_not_touched(self):
        rows, _ = w.make_predictions(Q, H, [run()], True, [])
        self.assertEqual(rows[0]['citations'], ['s1'])


if __name__ == '__main__':
    unittest.main()

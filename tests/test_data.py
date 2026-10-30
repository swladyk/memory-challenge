"""Windows, spans, dates, validation."""
import unittest
from datetime import date, datetime

from helpers import history, session
from memory_challenge.config import WindowConfig
from memory_challenge.data.dates import parse_dataset_date, question_time_range, resolve_relative
from memory_challenge.data.loader import parse_history
from memory_challenge.data.validation import validate_history
from memory_challenge.data.windows import build_windows, merge_spans, parse_blocks, render
from memory_challenge.schemas import Span

TIPS = ("Here are seven tips for better sleep:\n\n1. Keep a schedule.\n   Go to bed at the same time.\n"
        "2. Avoid caffeine after 2 pm.\n3. Dim the lights an hour before bed.\n4. No screens in bed.\n"
        "5. Keep the bedroom cool, around 18°C.\n6. Exercise early in the day.\n7. Try a warm bath before bed.\n\n"
        "Let me know if you want more!")


def content(turns):
    return parse_history(history("h", [session("s_a", "2023/05/20 (Sat) 02:21", turns)])).contents["s_a"]


class Windows(unittest.TestCase):
    def test_list_items_keep_ordinal_and_lead_in(self):
        c = content([("user", "How can I sleep better?"), ("assistant", TIPS)])
        ws = build_windows(c, WindowConfig(max_chars=120))
        seventh = [w for w in ws if any(i.ordinal == 7 for i in w.list_items)]
        self.assertTrue(seventh)
        text = render(c, list(seventh[0].spans))
        self.assertIn("7. Try a warm bath before bed.", text)
        self.assertIn("Here are seven tips for better sleep:", text)  # lead-in carried as context
        items = [b for b in parse_blocks(TIPS) if b.kind == "item"]
        self.assertEqual([b.ordinal for b in items], [1, 2, 3, 4, 5, 6, 7])
        self.assertIn("Go to bed at the same time.", TIPS[items[0].start:items[0].end])  # continuation stays with item

    def test_bullets_get_positions(self):
        text = "Options:\n- Lisbon\n- Porto\n- Faro"
        items = [b for b in parse_blocks(text) if b.kind == "item"]
        self.assertEqual([(b.ordinal, b.numbered) for b in items], [(1, False), (2, False), (3, False)])

    def test_short_reply_carries_previous_turn(self):
        c = content([("user", "My friend Rachel just moved."),
                     ("assistant", "Which city did Rachel move to? Tell me and I can check the weather."),
                     ("user", "She moved to Chicago.")])
        reply = [w for w in build_windows(c, WindowConfig()) if w.kind == "reply"][0]
        text = render(c, list(reply.spans))
        self.assertIn("assistant: Which city did Rachel move to?", text)
        self.assertIn("user: She moved to Chicago.", text)

    def test_spans_recover_original_text_exactly(self):
        c = content([("user", "x" * 50), ("assistant", TIPS * 3)])
        for w in build_windows(c, WindowConfig(max_chars=200)):
            for sp in w.spans:
                piece = c.turns[sp.turn_index].content[sp.start:sp.end]
                self.assertTrue(piece)
                self.assertIn(piece, c.turns[sp.turn_index].content)
                self.assertLessEqual(sp.end - sp.start, 200 if not sp.context else 10_000)

    def test_overlapping_windows_merge_without_duplication(self):
        c = content([("user", "q"), ("assistant", TIPS)])
        ws = build_windows(c, WindowConfig(max_chars=120, overlap_blocks=1))
        spans = [sp for w in ws[1:3] for sp in w.spans]
        merged = merge_spans(spans)
        text = render(c, spans)
        self.assertEqual(text.count("2. Avoid caffeine"), 1)
        for a, b in zip(merged, merged[1:]):
            self.assertTrue(a.turn_index != b.turn_index or a.end < b.start)

    def test_long_paragraph_is_split_at_sentences(self):
        long = " ".join(f"Sentence number {i} is here." for i in range(200))
        c = content([("user", long)])
        ws = build_windows(c, WindowConfig(max_chars=300))
        self.assertGreater(len(ws), 5)
        self.assertTrue(all(sp.end - sp.start <= 300 for w in ws for sp in w.spans))


class Dates(unittest.TestCase):
    def test_parse_and_weekday_check(self):
        self.assertEqual(parse_dataset_date("2023/05/20 (Sat) 02:21"), datetime(2023, 5, 20, 2, 21))
        self.assertIsNone(parse_dataset_date("2023/05/20 (Mon) 02:21"))
        self.assertIsNone(parse_dataset_date("yesterday"))

    def test_relative_resolution_table(self):
        anchor = datetime(2023, 5, 20, 10, 0)  # a Saturday
        cases = {"I went yesterday": (date(2023, 5, 19), "past"), "I'll go tomorrow": (date(2023, 5, 21), "planned"),
                 "We met last Friday": (date(2023, 5, 19), "past"), "two weeks ago": (date(2023, 5, 6), "past"),
                 "it's next Monday": (date(2023, 5, 22), "planned"), "on May 5th": (date(2023, 5, 5), "past"),
                 "I did it today": (date(2023, 5, 20), "mention")}
        for text, (want, kind) in cases.items():
            r = resolve_relative(text, anchor)
            self.assertEqual((r[0].start, r[0].kind), (want, kind), text)
        self.assertEqual(resolve_relative("December 28th", datetime(2023, 1, 4))[0].start, date(2022, 12, 28))
        self.assertEqual(resolve_relative("sometime soon, maybe", anchor), [])  # unresolvable: left as written

    def test_question_time_range(self):
        r = question_time_range("How many museums did I visit in December?", datetime(2023, 2, 1))
        self.assertEqual(r[:2], (date(2022, 12, 1), date(2022, 12, 31)))
        r = question_time_range("What did I bake in the past two weeks?", datetime(2023, 5, 30))
        self.assertEqual(r[:2], (date(2023, 5, 16), date(2023, 5, 30)))


class Validation(unittest.TestCase):
    def test_malformed_inputs_are_reported(self):
        self.assertTrue(validate_history({"sessions": []}))
        self.assertTrue(validate_history({"history_id": "h", "sessions": "nope"}))
        errs = validate_history(history("h", [{"session_id": "s1", "date": "bad", "turns": [{"role": "x"}]}]))
        self.assertTrue(any("date" in e for e in errs) and any("role" in e for e in errs))
        self.assertEqual(validate_history(history("h", [session("s1", "2023/05/20 (Sat) 02:21", [("user", "hi")])])), [])

    def test_empty_history_parses(self):
        h = parse_history(history("h_empty", []))
        self.assertEqual(h.occurrences, [])


if __name__ == "__main__":
    unittest.main()

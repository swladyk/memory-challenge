"""The mirrored message template must match the kit's own build_message character for character."""
import random
import unittest

import helpers  # noqa: F401  (sets sys.path)
from memory_challenge.reactor import message


class Mirror(unittest.TestCase):
    def test_mirror_matches_official(self):
        mod = message.official()
        if mod is None:
            self.skipTest("reactor/client.py could not be loaded")
        rng = random.Random(0)
        for _ in range(50):
            n = rng.randint(0, 6)
            passages = [{"id": f"s_{rng.getrandbits(48):012x}", "date": "2023/05/20 (Sat) 02:21",
                         "text": " ".join(rng.choice(["alpha", "beta", "[E2]", "ünï", "\n", "x" * 40]) for _ in range(30))}
                        for _ in range(n)]
            passages = message.normalize_passages(passages)
            for qd in ("2023/05/30 (Tue) 08:08", "2023-05-30", None):
                off = mod.build_message("What is my cat's name?\n\nNotes.", passages, message.REF,
                                        mod.parse_question_date(qd) if qd else None)
                mir = message.mirror_build_message("What is my cat's name?\n\nNotes.", passages, message.REF,
                                                   message.mirror_parse_question_date(qd) if qd else None)
                self.assertEqual(off, mir)

    def test_weekday_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            message.mirror_parse_question_date("2023/05/30 (Mon) 08:08")


if __name__ == "__main__":
    unittest.main()

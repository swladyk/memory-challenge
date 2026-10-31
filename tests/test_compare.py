"""Comparing systems with score.py's own functions: side-by-side scores, per-question transitions, payload evidence."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import write_jsonl
from memory_challenge.compare import compare, markdown
from memory_challenge.live import load_scorer

GOLD = [
    {"question_id": "q1", "question_type": "single-session-user", "abstain": False, "answer": "Luna", "aliases": [],
     "evidence": ["s1"]},
    {"question_id": "q2", "question_type": "multi-session", "abstain": False, "answer": "three", "aliases": ["3"],
     "evidence": ["s2", "s3"]},
    {"question_id": "q3", "question_type": "multi-session", "abstain": True, "answer": "", "aliases": [], "evidence": []},
    {"question_id": "q4", "question_type": "single-session-user", "abstain": False, "answer": "Paris", "aliases": [],
     "evidence": ["s4"]},
]
KIT = [  # wrong on q1, right on q2 with half the evidence, misses the abstention, no prediction for q4
    {"question_id": "q1", "abstain": False, "answer": "Bella", "citations": ["s9"]},
    {"question_id": "q2", "abstain": False, "answer": "3", "citations": ["s2"]},
    {"question_id": "q3", "abstain": False, "answer": "a dog", "citations": []},
]
OURS = [
    {"question_id": "q1", "abstain": False, "answer": "Her cat is Luna.", "citations": ["s1"]},
    {"question_id": "q2", "abstain": False, "answer": "three", "citations": ["s2", "s3"]},
    {"question_id": "q3", "abstain": True, "answer": "", "citations": []},
    {"question_id": "q4", "abstain": False, "answer": "probably somewhere in France", "citations": ["s4"]},
]


def payloads(root: Path, evidence: dict[str, list[str]]) -> Path:
    d = root / "payloads"
    d.mkdir(parents=True)
    for qid, ids in evidence.items():
        (d / f"{qid}.json").write_text(json.dumps({"question_id": qid, "message_chars": 100 * len(ids),
                                                   "evidence": [{"id": s, "text": "x"} for s in ids]}))
    return root


class Compare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_jsonl(self.root / "gold.jsonl", GOLD)
        write_jsonl(self.root / "kit.jsonl", KIT)
        write_jsonl(self.root / "ours.jsonl", OURS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scores_match_score_py_and_deltas_are_paired(self):
        report = compare(self.root / "gold.jsonl", {"kit": self.root / "kit.jsonl", "ours": self.root / "ours.jsonl"})
        scorer = load_scorer()
        for label, preds in (("kit", KIT), ("ours", OURS)):  # exactly what score.py's main() would print
            by_id = {p["question_id"]: p for p in preds}
            expected = scorer.summarize([scorer.score_question(by_id.get(g["question_id"]), g) for g in GOLD])
            for t, row in expected.items():
                for k, v in row.items():
                    got = report["scores"][label][t][k]
                    self.assertAlmostEqual(got, v, places=4) if isinstance(v, float) else self.assertEqual(got, v)
        p = report["paired"]["ours"]
        self.assertEqual(p["transitions"]["unjudged"]["correct"], 1)  # q1: score.py leaves "Bella" to the judge
        self.assertEqual(p["transitions"]["correct"]["correct"], 1)  # q2
        self.assertEqual(p["transitions"]["wrong"]["abstained-ok"], 1)  # q3: kit answered an unanswerable question
        self.assertEqual(p["transitions"]["missing"]["unjudged"], 1)  # q4: no kit prediction; ours is not exact
        self.assertEqual((p["correct"]["gained"], p["correct"]["lost"]), (2, 0))  # q1, q3
        self.assertEqual((p["evidence_f1"]["better"], p["evidence_f1"]["worse"]), (3, 0))
        self.assertEqual(len(p["correct"]["ci95"]), 2)
        self.assertEqual(report["reference"], "kit")

    def test_reference_option_and_invalid_predictions(self):
        report = compare(self.root / "gold.jsonl", {"kit": self.root / "kit.jsonl", "ours": self.root / "ours.jsonl"},
                         reference="ours")
        self.assertEqual((report["reference"], list(report["paired"])), ("ours", ["kit"]))
        write_jsonl(self.root / "bad.jsonl", KIT + [{"question_id": "q_other", "abstain": False, "answer": "x",
                                                     "citations": []}])
        with self.assertRaises(ValueError):
            compare(self.root / "gold.jsonl", {"kit": self.root / "bad.jsonl", "ours": self.root / "ours.jsonl"})
        with self.assertRaises(ValueError):
            compare(self.root / "gold.jsonl", {"ours": self.root / "ours.jsonl"})

    def test_payload_comparison_needs_no_predictions(self):
        kit = payloads(self.root / "kit", {"q1": ["s9"], "q2": ["s2", "s8"], "q3": ["s5"], "q4": ["s7"]})
        ours = payloads(self.root / "ours", {"q1": ["s1", "s9"], "q2": ["s2", "s3"], "q3": ["s5"], "q4": ["s4"]})
        report = compare(self.root / "gold.jsonl", payloads={"kit": kit, "ours": ours})
        self.assertEqual(report["scores"], {})
        self.assertEqual(report["retrieval"]["kit"]["all"]["gold_recall"], round((0 + 0.5 + 0) / 3, 4))
        self.assertEqual(report["retrieval"]["ours"]["all"]["gold_recall"], 1.0)
        self.assertEqual(report["retrieval"]["ours"]["all"]["all_gold"], 1.0)
        md = markdown(report)
        self.assertIn("What each system sent", md)
        self.assertNotIn("Scores by question type", md)

    def test_markdown_has_side_by_side_tables(self):
        report = compare(self.root / "gold.jsonl", {"kit": self.root / "kit.jsonl", "ours": self.root / "ours.jsonl"})
        md = markdown(report)
        self.assertIn("correct · kit | correct · ours", md)
        self.assertIn("ours against kit, question by question", md)
        self.assertIn("| `q4` |", md)


if __name__ == "__main__":
    unittest.main()

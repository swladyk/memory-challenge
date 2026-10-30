"""Supported-citation fallback for answers without usable [E#] labels, and the scheduler's retry budget."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import write_jsonl
from memory_challenge.config import Config
from memory_challenge.evaluation.runner import run_split
from memory_challenge.reactor import checkpoints
from memory_challenge.reactor.backends import FakeBackend
from memory_challenge.reactor.citation_fallback import answer_units, choose
from memory_challenge.reactor.predictions import export, validate
from memory_challenge.reactor.scheduler import load_payloads, run
from test_reactor import make_split

NO_LABEL = {"status": "answered", "citations": [], "unknown_citations": [], "uncited": True, "abstained": False}


class Units(unittest.TestCase):
    def test_distinctive_units(self):
        units = {u: d for _, u, d in answer_units("Your best 5K time is 25:50, about 3 minutes faster, at Target.")}
        self.assertTrue(units["25:50"])  # number with a separator
        self.assertFalse(units["3"])  # a single digit never supports an answer on its own
        self.assertTrue(units["target"])  # capitalised in the answer
        self.assertNotIn("your", units)

    def test_choose_supported_unsupported_foreign(self):
        payload = {"evidence": [
            {"id": "s_a", "text": "user: My personal best in the charity 5K is 25:50 now.\n[note: session dated 2 days before]"},
            {"id": "s_b", "text": "user: I like running in the park."},
            {"id": "s_x", "text": "user: My personal best in the charity 5K is 25:50."}]}
        chosen, rec = choose("25:50 (personal best in the charity 5K)", payload, {"s_a", "s_b"})
        self.assertEqual(chosen, ["s_a"])  # s_x supports it but is not in this history
        self.assertIn({"session": "s_x", "reason": "not in this question's history"}, rec["rejected"])
        chosen, rec = choose("Mittens", payload, {"s_a", "s_b"})
        self.assertEqual(chosen, [])  # nothing supports it: no fallback to the top passage
        self.assertIn("left empty", rec["reason"])
        chosen, _ = choose("3", {"evidence": [{"id": "s_a", "text": "user: I have 3 cats and 3 dogs."}]}, {"s_a"})
        self.assertEqual(chosen, [])  # a lone single digit is not distinctive
        notes_only = {"evidence": [{"id": "s_a", "text": "user: hello\n[note: 'yesterday' = Sat 2023-05-20]"}]}
        self.assertEqual(choose("2023-05-20", notes_only, {"s_a"})[0], [])  # computed notes are not evidence


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.split = make_split(self.root)
        run_split(self.split, Config(), workers=1, out_dir=self.root / "retrieve")
        self.payloads = load_payloads(self.root / "retrieve" / "payloads")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fallback_after_kit_conversion(self):
        script = {"q_0_cat": [{**NO_LABEL, "answer": "Your cat is called Luna0."}],
                  "q_1_cat": [{**NO_LABEL, "answer": "Mittens"}],
                  "q_2_cat": [{**NO_LABEL, "answer": "Luna0", "uncited": False, "unknown_citations": ["E7"]}],
                  "q_2_job": [{"status": "answered", "answer": "INSUFFICIENT_EVIDENCE", "abstained": True,
                               "citations": [], "unknown_citations": []}]}
        run(self.payloads, [FakeBackend("agt_a", script=script)], self.root / "run", backoff_s=0)
        # q_2_cat: tamper its payload so the only passage containing the answer is another user's session.
        pdir = self.root / "retrieve" / "payloads"
        p2 = json.loads((pdir / "q_2_cat.json").read_text())
        p2["evidence"] = [e for e in p2["evidence"] if "Luna2" not in e["text"]] + [
            {"id": "s_0_cat", "date": "2023/05/20 (Sat) 02:21", "text": "user: My cat is called Luna0."}]
        (pdir / "q_2_cat.json").write_text(json.dumps(p2))
        out = self.root / "predictions.jsonl"
        rep = export(self.root / "run" / "results.jsonl", self.split / "histories.jsonl", out, payloads=pdir)
        preds = {json.loads(l)["question_id"]: json.loads(l) for l in out.read_text().splitlines()}
        kit = {json.loads(l)["question_id"]: json.loads(l) for l in Path(str(out) + ".kit.jsonl").read_text().splitlines()}
        self.assertEqual(kit["q_0_cat"]["citations"], [])
        self.assertEqual(preds["q_0_cat"]["citations"], ["s_0_cat"])  # supported → filled
        self.assertEqual(preds["q_0_cat"]["answer"], kit["q_0_cat"]["answer"])  # answer text untouched
        self.assertEqual(preds["q_1_cat"]["citations"], [])  # unsupported → stays empty
        self.assertEqual(preds["q_2_cat"]["citations"], [])  # never another history's session
        self.assertTrue(preds["q_2_job"]["abstain"])
        self.assertEqual(preds["q_2_job"]["citations"], [])  # abstentions untouched
        self.assertEqual(preds["q_0_job"]["citations"], kit["q_0_job"]["citations"])  # existing citations untouched
        log = json.loads(Path(str(out) + ".citation_fallback.json").read_text())
        by_q = {d["question_id"]: d for d in log["decisions"]}
        self.assertEqual(by_q["q_0_cat"]["why_no_citation"], "no [E#] label in the final answer")
        self.assertIn("s_0_cat", by_q["q_0_cat"]["support"])
        self.assertEqual(by_q["q_2_cat"]["why_no_citation"], "labels never sent: ['E7']")
        self.assertEqual(rep["citation_fallback"]["filled"], 1)
        self.assertTrue(validate(out, self.split / "questions.jsonl", self.split / "histories.jsonl")["valid"])


class RetryBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        split = make_split(self.root)
        run_split(split, Config(), workers=1, out_dir=self.root / "retrieve")
        self.payloads = load_payloads(self.root / "retrieve" / "payloads")

    def tearDown(self):
        self.tmp.cleanup()

    def test_previous_pending_retried_with_default_budget(self):
        pending = {"status": "refused", "code": "previous_pending", "forwarded": False}
        self.assertEqual((Config().reactor.retry_refused, Config().reactor.refused_backoff_s), (3, 7.0))
        script = {"q_0_cat": [pending] * 3, "q_1_cat": [pending] * 4}
        run(self.payloads, [FakeBackend("agt_a", script=script)], self.root / "run",
            retry_refused=Config().reactor.retry_refused, backoff_s=0)
        acc = checkpoints.accepted(checkpoints.AttemptLog(self.root / "run" / "attempts.jsonl").load())
        self.assertEqual((acc["q_0_cat"]["outcome"], acc["q_0_cat"]["attempt"]), ("answered", 4))
        self.assertEqual((acc["q_1_cat"]["outcome"], acc["q_1_cat"]["attempt"]), ("not_delivered", 4))


if __name__ == "__main__":
    unittest.main()

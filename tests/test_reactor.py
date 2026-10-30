"""Reactor harness with the fake backend: scheduling, retries, resume, late replies, export, validation."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import filler, history, session, write_jsonl
from memory_challenge.config import Config
from memory_challenge.evaluation.runner import run_split
from memory_challenge.reactor import checkpoints
from memory_challenge.reactor.backends import FakeBackend
from memory_challenge.reactor.predictions import export, validate
from memory_challenge.reactor.scheduler import load_payloads, run

D = "2023/05/20 (Sat) 02:21"
QD = "2023/05/30 (Tue) 08:08"


def make_split(root: Path) -> Path:
    split = root / "split"
    split.mkdir()
    hs, qs = [], []
    for h in range(3):
        hid = f"h_{h}"
        sessions = filler(f"{h}x", D) + [
            # U+2028 occurs in every released split; the kit's predictions.py splits lines on it (see export()).
            session(f"s_{h}_cat", D, [("user", f"My cat is called Luna{h}. She is three."), ("assistant", "Lovely name!")]),
            session(f"s_{h}_job", D, [("user", f"I started a new job as a baker number {h}."), ("assistant", "Congrats!")])]
        hs.append(history(hid, sessions))
        qs.append({"question_id": f"q_{h}_cat", "history_id": hid, "question": "What is my cat's name?", "question_date": QD})
        qs.append({"question_id": f"q_{h}_job", "history_id": hid, "question": "What is my new job?", "question_date": QD})
    write_jsonl(split / "histories.jsonl", hs)
    write_jsonl(split / "questions.jsonl", qs)
    return split


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.split = make_split(self.root)
        self.retrieval = self.root / "retrieve"
        run_split(self.split, Config(), workers=1, out_dir=self.retrieval)
        order = [json.loads(l)["question_id"] for l in (self.split / "questions.jsonl").read_text().splitlines()]
        self.payloads = load_payloads(self.retrieval / "payloads", order)

    def tearDown(self):
        self.tmp.cleanup()

    def attempts(self, run_dir):
        return checkpoints.AttemptLog(run_dir / "attempts.jsonl").load()

    def test_sequential_one_history_per_dataset_history(self):
        agents = [FakeBackend("agt_a"), FakeBackend("agt_b")]
        summary = run(self.payloads, agents, self.root / "run")
        self.assertEqual(summary["outcomes"], {"answered": 6})
        self.assertEqual(sum(a.histories for a in agents), 3)
        self.assertTrue(all(a.max_in_flight <= 1 for a in agents))
        for a in agents:  # within an agent: new_history, then that history's questions in file order
            seq = [c for c in a.calls]
            for i, (kind, val) in enumerate(seq):
                if kind == "new_history":
                    asks = [v for k, v in seq[i + 1:i + 3]]
                    self.assertEqual(asks[0].rsplit("_", 1)[0], asks[1].rsplit("_", 1)[0])
                    self.assertEqual([x.rsplit("_", 1)[1] for x in asks], ["cat", "job"])
        lines = (self.root / "run" / "results.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), len({json.loads(l)["question_id"] for l in lines}))

    def test_safe_retry_of_refusals_never_forwarded(self):
        script = {"q_0_cat": [{"status": "refused", "code": "busy", "forwarded": False}]}
        run(self.payloads, [FakeBackend("agt_a", script=script)], self.root / "run", retry_refused=1, backoff_s=0)
        acc = checkpoints.accepted(self.attempts(self.root / "run"))
        self.assertEqual(acc["q_0_cat"]["outcome"], "answered")
        self.assertEqual(acc["q_0_cat"]["attempt"], 2)  # never forwarded: retried

    def test_late_final_answer_is_salvaged_for_its_own_question(self):
        script = {"q_0_cat": [{"status": "timeout"}], "q_0_job": [{"status": "answered", "answer": "a baker [E1]",
                                                                   "citations": [], "abstained": False, "late_for": "q_0_cat"}]}
        run(self.payloads, [FakeBackend("agt_a", script=script)], self.root / "run")
        log = self.attempts(self.root / "run")
        acc = checkpoints.accepted(log)
        self.assertEqual(acc["q_0_cat"]["outcome"], "answered_late")
        self.assertEqual(acc["q_0_cat"]["client"]["answer"], "a late answer")
        self.assertEqual(acc["q_0_cat"]["seen_during"], "q_0_job")
        timeouts = [a for a in log if a.get("kind") == "ask" and a.get("question_id") == "q_0_cat"
                    and a["outcome"] == "ambiguous"]
        self.assertEqual(len(timeouts), 1)  # the timeout attempt stays in the log

    def test_resume_skips_final_and_restarts_history_for_another_agent(self):
        script = {"q_2_job": [{"status": "closed"}, {"status": "closed"}]}  # the re-ask fails too
        run(self.payloads, [FakeBackend("agt_a", script=script)], self.root / "run")
        second = FakeBackend("agt_b")
        summary = run(self.payloads, [second], self.root / "run")
        self.assertEqual([c for c in second.calls if c[0] == "ask"], [("ask", "q_2_job")])
        self.assertEqual(second.histories, 1)  # agt_a's history isn't agt_b's: a new one for the history left
        self.assertEqual(summary["outcomes"].get("answered"), 6)

    def test_mixed_configurations_are_refused(self):
        other = [dict(p, config_hash="different") for p in self.payloads[:1]]
        with self.assertRaises(ValueError):
            run(self.payloads + other, [FakeBackend()], self.root / "run")

    def test_citation_labels_are_per_question(self):
        run(self.payloads, [FakeBackend("agt_a")], self.root / "run")
        acc = checkpoints.accepted(self.attempts(self.root / "run"))
        by_q = {q: a["client"]["citations"] for q, a in acc.items()}
        for p in self.payloads:
            labels = {m["label"]: m["session_id"] for m in p["manifest"]}
            for c in by_q[p["question_id"]]:
                self.assertEqual(labels[c["label"]], c["id"])
        self.assertNotEqual(by_q["q_0_cat"][0]["id"], by_q["q_1_cat"][0]["id"])

    def test_export_and_validate_with_the_kit_converter(self):
        script = {"q_0_cat": [{"status": "answered", "answer": "INSUFFICIENT_EVIDENCE", "abstained": True,
                               "citations": [], "unknown_citations": []}],
                  "q_0_job": [{"status": "timeout"}, {"status": "timeout"}],  # the same-history re-ask too
                  "q_1_cat": [{"status": "answered", "answer": "Luna1 [E9]", "abstained": False,
                               "citations": [], "unknown_citations": ["E9"]}]}
        run(self.payloads, [FakeBackend("agt_a", script=script)], self.root / "run")
        out = self.root / "predictions.jsonl"
        rep = export(self.root / "run" / "results.jsonl", self.split / "histories.jsonl", out)
        preds = {json.loads(l)["question_id"]: json.loads(l) for l in out.read_text().splitlines()}
        self.assertTrue(preds["q_0_cat"]["abstain"])  # Reactor's INSUFFICIENT_EVIDENCE
        self.assertNotIn("q_0_job", preds)  # a timeout is not an abstention: reported as skipped
        self.assertTrue(any("q_0_job" in s for s in rep["skipped"]))
        self.assertEqual(preds["q_1_cat"]["citations"], [])  # unknown label never mapped
        v = validate(out, self.split / "questions.jsonl", self.split / "histories.jsonl")
        self.assertTrue(v["valid"])
        self.assertEqual(v["missing"], 1)
        bad = dict(preds["q_1_cat"], citations=["s_0_cat"])  # a session of another user's history
        write_jsonl(out, [bad])
        v = validate(out, self.split / "questions.jsonl", self.split / "histories.jsonl")
        self.assertFalse(v["valid"])
        self.assertIn("not in its own history", v["errors"][0])
        write_jsonl(out, [preds["q_1_cat"], preds["q_1_cat"]])
        self.assertIn("duplicate", " ".join(validate(out, self.split / "questions.jsonl", self.split / "histories.jsonl")["errors"]))


if __name__ == "__main__":
    unittest.main()

"""Live run logs: append-only events and stats history with staged progress, for retrieval and Reactor runs."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path

from helpers import write_jsonl
from memory_challenge import cli
from memory_challenge.config import Config
from memory_challenge.evaluation.runner import run_split
from memory_challenge.live import LiveRun
from memory_challenge.reactor.backends import FakeBackend
from memory_challenge.reactor.scheduler import load_payloads, run
from test_reactor import make_split

TEXT = ("Luna", "baker", "What is my", "cat is called")  # conversation, question and gold text: never in the logs


def lines(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines()]


def add_gold(split: Path) -> dict:
    gold = []
    for h in range(3):
        gold.append({"question_id": f"q_{h}_cat", "question_type": "single-session-user", "abstain": False,
                     "answer": f"Luna{h}", "aliases": [], "evidence": [f"s_{h}_cat"]})
        gold.append({"question_id": f"q_{h}_job", "question_type": "multi-session", "abstain": h == 2,
                     "answer": "baker" if h < 2 else "", "aliases": [], "evidence": [] if h == 2 else [f"s_{h}_job"]})
    write_jsonl(split / "gold.jsonl", gold)
    return {g["question_id"]: g for g in gold}


class LiveRunFiles(unittest.TestCase):
    def test_append_only_meta_and_resumed_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "r"
            live = LiveRun(d, "retrieval", {"split": "x"})
            live.event({"stage": "processed", "question_id": "q1"})
            live.event({"stage": "processed", "question_id": "q2"})
            live.snapshot({"progress": {"processed": 2}})
            live.finish(questions=2)
            meta = json.loads((d / "run.json").read_text())
            self.assertEqual((meta["status"], meta["run_id"], meta["questions"], meta["schema"]),
                             ("finished", "r", 2, "memret.live.v1"))
            again = LiveRun(d, "retrieval", {})  # a resumed run keeps appending with increasing seq
            again.event({"stage": "processed", "question_id": "q3"})
            self.assertEqual([e["seq"] for e in lines(d / "events.jsonl")], [1, 2, 3])
            self.assertEqual(len(lines(d / "stats_history.jsonl")), 1)
            self.assertFalse((d / "run.json.tmp").exists())


class RetrievalLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.split = make_split(self.root)
        add_gold(self.split)

    def tearDown(self):
        self.tmp.cleanup()

    def evaluate(self, workers):
        out = self.root / f"eval{workers}"
        args = argparse.Namespace(partition="all", limit=None, workers=workers)
        summary, scored = cli._evaluate(self.split, Config(), args, None, live_dir=out)
        return out, summary, scored

    def test_one_event_and_snapshot_per_question_with_staged_progress(self):
        for workers in (1, 2):
            out, summary, scored = self.evaluate(workers)
            events, stats = lines(out / "events.jsonl"), lines(out / "stats_history.jsonl")
            self.assertEqual(len(events), 6)
            self.assertEqual(len(stats), 6)
            self.assertEqual(sorted(e["question_id"] for e in events), sorted(s["question_id"] for s in scored))
            self.assertTrue(all(e["stage"] == "graded" for e in events))
            prog = [s["progress"] for s in stats]
            for key in ("processed", "evaluated", "graded"):
                self.assertEqual([p[key] for p in prog], list(range(1, 7)))
            self.assertTrue(all(p["total"] == 6 for p in prog))
            self.assertEqual(prog[-1]["eta_s"], 0.0)
            last = stats[-1]["metrics"]
            self.assertEqual(last["all"]["pack_all"], summary["all"]["pack_all"])  # the stream ends at the summary
            self.assertEqual(last["all"]["n"], 6)
            meta = json.loads((out / "run.json").read_text())
            self.assertEqual((meta["status"], meta["total"], meta["has_gold"], meta["kind"]),
                             ("finished", 6, True, "retrieval"))
            raw = (out / "events.jsonl").read_text() + (out / "stats_history.jsonl").read_text()
            self.assertFalse([t for t in TEXT if t in raw])

    def test_retrieve_without_gold_reports_no_grading(self):
        out = self.root / "ret"
        rc = cli.main(["retrieve", "--split", str(self.split), "--out", str(out), "--workers", "1"])
        self.assertEqual(rc, 0)
        stats = lines(out / "stats_history.jsonl")
        self.assertEqual(stats[-1]["progress"]["processed"], 6)
        self.assertIsNone(stats[-1]["progress"]["graded"])
        self.assertNotIn("metrics", stats[-1])
        self.assertEqual(json.loads((out / "run.json").read_text())["has_gold"], False)


class ReactorLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        split = make_split(self.root)
        self.gold = add_gold(split)
        run_split(split, Config(), workers=1, out_dir=self.root / "retrieve")
        self.payloads = load_payloads(self.root / "retrieve" / "payloads")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sent_attempt_and_graded_events_with_local_scores(self):
        script = {"q_0_cat": [{"status": "refused", "code": "busy", "forwarded": False}]}
        d = self.root / "run"
        run(self.payloads, [FakeBackend("agt_a", script=script)], d, retry_refused=1, backoff_s=0, gold=self.gold,
            live_meta={"command": "test"})
        events, stats = lines(d / "events.jsonl"), lines(d / "stats_history.jsonl")
        self.assertEqual(len(events), len(stats))
        q0 = [e["stage"] for e in events if e["question_id"] == "q_0_cat"]
        self.assertEqual(q0, ["processed", "attempt", "processed", "graded"])  # refused once, then answered
        graded = [e for e in events if e["stage"] == "graded"]
        self.assertEqual(len(graded), 6)
        self.assertTrue(all("local_unofficial" in e for e in graded))
        prog = [s["progress"] for s in stats]
        for key in ("processed", "evaluated", "graded"):
            seq = [p[key] for p in prog]
            self.assertEqual(seq, sorted(seq))
            self.assertEqual(seq[-1], 6)
        self.assertTrue(all(p["processed"] >= p["evaluated"] >= p["graded"] for p in prog))
        last = stats[-1]
        self.assertEqual(last["outcomes"], {"answered": 6})
        self.assertEqual(last["metrics"]["all"]["n"], 6)
        self.assertIn("unofficial", last["metrics_label"])
        meta = json.loads((d / "run.json").read_text())
        self.assertEqual((meta["status"], meta["kind"], meta["command"]), ("finished", "reactor", "test"))
        raw = (d / "events.jsonl").read_text() + (d / "stats_history.jsonl").read_text()
        self.assertFalse([t for t in TEXT if t in raw])

    def test_without_gold_nothing_is_graded(self):
        d = self.root / "run"
        run(self.payloads, [FakeBackend("agt_a")], d)
        stats = lines(d / "stats_history.jsonl")
        self.assertIsNone(stats[-1]["progress"]["graded"])
        self.assertEqual(stats[-1]["progress"]["evaluated"], 6)
        self.assertNotIn("metrics", stats[-1])


if __name__ == "__main__":
    unittest.main()

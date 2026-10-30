"""Timeouts as the organisers describe them (2026-10-31): Reactor keeps working after the client stops waiting, so a
late final is salvaged from a later request in the same history; otherwise one ping, then one re-ask in the SAME
Reactor history. Resume continues the stored history unless the server says history_changed."""
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts src/ on the path)
from memory_challenge.config import Config
from memory_challenge.evaluation.runner import run_split
from memory_challenge.reactor import checkpoints
from memory_challenge.reactor.backends import FakeBackend
from memory_challenge.reactor.scheduler import load_payloads, run
from test_reactor import make_split


class Timeouts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        split = make_split(self.root)
        run_split(split, Config(), workers=1, out_dir=self.root / "retrieve")
        order = [json.loads(l)["question_id"] for l in (split / "questions.jsonl").read_text().splitlines()]
        self.payloads = load_payloads(self.root / "retrieve" / "payloads", order)
        self.by_q = {p["question_id"]: p for p in self.payloads}
        self.run_dir = self.root / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def log(self):
        return checkpoints.AttemptLog(self.run_dir / "attempts.jsonl").load()

    def run_with(self, backend, **kw):
        return run(self.payloads, [backend], self.run_dir, backoff_s=0, **kw)

    def labels(self, qid):
        return {m["label"]: m["session_id"] for m in self.by_q[qid]["manifest"]}

    def test_timeout_then_late_reply_is_salvaged_with_its_own_citations(self):
        agent = FakeBackend("agt_a", script={"q_0_cat": [{"status": "timeout", "late": True}]})
        self.run_with(agent)
        acc = checkpoints.accepted(self.log())
        got = acc["q_0_cat"]
        self.assertEqual(got["outcome"], "answered_late")
        self.assertEqual(got["client"]["status"], "answered")
        self.assertFalse(got["client"]["abstained"])
        cites = got["client"]["citations"]
        self.assertTrue(cites)
        for c in cites:  # labels mapped against q_0_cat's passages, not q_0_job's (the request it arrived with)
            self.assertEqual(self.labels("q_0_cat")[c["label"]], c["id"])
        self.assertEqual(got["seen_during"], "q_0_job")
        self.assertEqual([c for c in agent.calls if c[0] == "ask"].count(("ask", "q_0_cat")), 1)  # never re-asked
        self.assertEqual(agent.histories, 3)
        self.assertNotIn("text", [c[0] for c in agent.calls])  # nothing left to drain
        events = [json.loads(l) for l in (self.run_dir / "events.jsonl").read_text().splitlines()]
        salvaged = [e for e in events if e.get("late_salvaged")]
        self.assertEqual([e["question_id"] for e in salvaged], ["q_0_cat"])
        last = json.loads((self.run_dir / "stats_history.jsonl").read_text().splitlines()[-1])
        self.assertEqual(last["progress"]["late_salvaged"], 1)
        self.assertEqual(last["outcomes"]["answered_late"], 1)
        results = [json.loads(l) for l in (self.run_dir / "results.jsonl").read_text().splitlines()]
        self.assertEqual(len(results), 6)

    def test_late_abstention_is_salvaged_as_abstained(self):
        agent = FakeBackend("agt_a", policy=lambda q, p: (None, []),
                            script={"q_1_cat": [{"status": "timeout", "late": True}]})
        self.run_with(agent)
        got = checkpoints.accepted(self.log())["q_1_cat"]
        self.assertEqual(got["outcome"], "answered_late")
        self.assertTrue(got["client"]["abstained"])

    def test_last_question_of_a_history_is_drained_by_the_ping(self):
        agent = FakeBackend("agt_a", script={"q_0_job": [{"status": "timeout", "late": True}]})
        self.run_with(agent)
        got = checkpoints.accepted(self.log())["q_0_job"]
        self.assertEqual((got["outcome"], got["via"]), ("answered_late", "ping"))
        calls = agent.calls
        i = calls.index(("ask", "q_0_job"))
        self.assertEqual(calls[i + 1], ("text", "Reply only OK."))
        self.assertEqual(calls[i + 2][0], "new_history")  # salvaged: no re-ask
        pings = [a for a in self.log() if a.get("kind") == "ping"]
        self.assertEqual((len(pings), pings[0]["late_replies"]), (1, 1))

    def test_timeout_without_late_reply_is_reasked_once_in_the_same_history(self):
        agent = FakeBackend("agt_a", script={"q_0_cat": [{"status": "timeout"}]})
        self.run_with(agent)
        h0 = agent.calls[:agent.calls.index(("new_history", "fake-agt_a-2"))]
        self.assertEqual(h0, [("new_history", "fake-agt_a-1"), ("ask", "q_0_cat"), ("ask", "q_0_job"),
                              ("text", "Reply only OK."), ("ask", "q_0_cat")])
        self.assertEqual(agent.histories, 3)  # one per dataset history: never a fresh one for the re-ask
        got = checkpoints.accepted(self.log())["q_0_cat"]
        self.assertEqual((got["outcome"], got["attempt"], got["reask"]), ("answered", 2, True))
        reask = [a for a in self.log() if a.get("kind") == "ask" and a.get("reask")]
        self.assertEqual({a["reactor_history_id"] for a in reask}, {"fake-agt_a-1"})

    def test_reask_that_times_out_again_stays_a_timeout(self):
        agent = FakeBackend("agt_a", script={"q_0_cat": [{"status": "timeout"}, {"status": "timeout"}]})
        summary = self.run_with(agent)
        asks = [c for c in agent.calls if c == ("ask", "q_0_cat")]
        self.assertEqual(len(asks), 2)  # asked once, re-asked once
        self.assertEqual([c for c in agent.calls[:8] if c[0] == "text"], [("text", "Reply only OK.")] * 2)
        self.assertEqual(checkpoints.accepted(self.log())["q_0_cat"]["outcome"], "ambiguous")
        self.assertEqual(summary["outcomes"], {"answered": 5, "ambiguous": 1})
        self.assertEqual(agent.histories, 3)
        meta = json.loads((self.run_dir / "run.json").read_text())
        last = json.loads((self.run_dir / "stats_history.jsonl").read_text().splitlines()[-1])
        self.assertEqual((meta["status"], last["progress"]["evaluated"], last["progress"]["failures"]),
                         ("finished", 6, 1))

    def test_resume_continues_the_stored_reactor_history(self):
        agent = FakeBackend("agt_a", script={"q_2_job": [{"status": "error", "error": "client crashed"}]})
        self.run_with(agent)
        self.assertEqual(agent.histories, 3)
        agent.calls.clear()
        summary = self.run_with(agent)  # same agent, still on the last dataset history's Reactor history
        self.assertEqual(agent.calls, [("ask", "q_2_job")])  # no new history
        self.assertEqual(summary["outcomes"].get("answered"), 6)
        reuse = [a for a in self.log() if a.get("kind") == "history_reuse"]
        self.assertEqual((len(reuse), reuse[0]["reactor_history_id"]), (1, "fake-agt_a-3"))
        got = checkpoints.accepted(self.log())["q_2_job"]
        self.assertEqual((got["attempt"], got["reactor_history_id"]), (2, "fake-agt_a-3"))

    def test_resume_salvages_an_earlier_runs_timeout_in_the_stored_history(self):
        agent = FakeBackend("agt_a", script={"q_2_job": [{"status": "timeout", "late": True}],
                                             "__ping__": [{"status": "timeout", "hold": True}]})  # not produced yet
        self.run_with(agent, reask=0)
        self.assertEqual(checkpoints.accepted(self.log())["q_2_job"]["outcome"], "ambiguous")
        agent.calls.clear()
        self.run_with(agent)  # the late final comes with the next request in that history
        got = checkpoints.accepted(self.log())["q_2_job"]
        self.assertEqual(got["outcome"], "answered")  # this run's own answer outranks the salvaged one
        salvaged = [a for a in self.log() if a.get("salvaged")]
        self.assertEqual([a["question_id"] for a in salvaged], ["q_2_job"])
        self.assertEqual(agent.histories, 3)

    def test_history_changed_on_resume_starts_a_new_history_and_logs_why(self):
        agent = FakeBackend("agt_a", script={"q_2_job": [{"status": "error", "error": "client crashed"}]})
        self.run_with(agent)
        agent.new_history()  # someone else used the agent in between: our stored history is gone
        agent.calls.clear()
        summary = self.run_with(agent)
        self.assertEqual(agent.calls, [("ask", "q_2_job"), ("new_history", "fake-agt_a-5"), ("ask", "q_2_job")])
        self.assertEqual(summary["outcomes"].get("answered"), 6)
        nh = [a for a in self.log() if a.get("kind") == "new_history"][-1]
        self.assertEqual(nh["previous_reactor_history_id"], "fake-agt_a-3")
        self.assertIn("history_changed", nh["reason"])

    def test_history_changed_mid_run_stops_instead_of_resetting_someone_elses_history(self):
        taken = {"status": "refused", "code": "history_changed", "forwarded": False, "current_history_id": "theirs"}
        agent = FakeBackend("agt_a", script={"q_0_job": [taken]})
        summary = self.run_with(agent)
        self.assertEqual(agent.histories, 1)
        self.assertIn("another process", summary["stopped"])
        self.assertEqual([a["kind"] for a in self.log()].count("stopped"), 1)
        self.assertEqual(json.loads((self.run_dir / "run.json").read_text())["status"], "stopped")


    def test_smoke_check_stops_after_a_failed_first_question(self):
        refused = {"status": "refused", "code": "permission", "text": "no access", "forwarded": False}
        agent = FakeBackend("agt_a", script={"q_0_cat": [refused]})
        summary = self.run_with(agent, smoke_check=True)
        self.assertEqual([c for c in agent.calls if c[0] == "ask"], [("ask", "q_0_cat")])  # nothing else asked
        self.assertEqual(agent.histories, 1)
        self.assertIn("smoke check", summary["stopped"])
        self.assertEqual(json.loads((self.run_dir / "run.json").read_text())["status"], "stopped")

    def test_smoke_check_passes_on_a_first_timeout_or_answer(self):
        agent = FakeBackend("agt_a", script={"q_0_cat": [{"status": "timeout", "late": True}],
                                             "q_1_cat": [{"status": "error", "error": "boom"}]})
        summary = self.run_with(agent, smoke_check=True)  # later failures don't stop the run
        self.assertIsNone(summary["stopped"])
        self.assertEqual(summary["outcomes"], {"answered": 4, "answered_late": 1, "error": 1})


if __name__ == "__main__":
    unittest.main()

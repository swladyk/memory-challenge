"""The halo.harness.v1 adapter: contract, history binding, failure modes, and the team dashboard's own client."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import REPO
from memory_challenge.config import Config
from memory_challenge.harness import CONTRACT, HarnessApp, HarnessError, serve
from test_reactor import QD, make_split


def request(qid="q_0_cat", hid="h_0", prompt="What is my cat's name?"):
    return {"contract_version": CONTRACT, "run_id": "run_" + "a" * 32, "question_id": qid, "original_prompt": prompt,
            "question_date": QD, "evidence": [], "context": {"dataset_history_id": hid}}


class Adapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.split = make_split(self.root)
        self.app = HarnessApp(self.split, Config(), self.root / "out")

    def tearDown(self):
        self.tmp.cleanup()

    def test_contract_response_is_bound_to_the_dataset_history(self):
        res = self.app.prepare(request())
        self.assertEqual(res["contract_version"], CONTRACT)
        self.assertTrue(res["prompt"].startswith("What is my cat's name?"))
        self.assertEqual(res["question_date"], QD)  # unchanged: the team's export rejects a changed date
        ids = [e["id"] for e in res["evidence"]]
        self.assertIn("s_0_cat", ids)
        self.assertTrue(all(i.startswith("s_0") for i in ids))  # only history h_0's sessions
        self.assertTrue(all(e["date"] == "2023/05/20 (Sat) 02:21" for e in res["evidence"]))
        self.assertTrue((self.root / "out" / "payloads" / "q_0_cat.json").exists())  # for `memret why` and fallback

    def test_failure_modes_fail_closed(self):
        for bad, status in ((dict(request(), contract_version="v0"), 400), (dict(request(), original_prompt=" "), 400),
                            (dict(request(), context={}), 422), (request(hid="h_missing"), 422),
                            (dict(request(), question_date="2023/05/30 (Mon) 08:08"), 400)):
            with self.assertRaises(HarnessError) as ctx:
                self.app.prepare(bad)
            self.assertEqual(ctx.exception.status, status)

    def test_http_token_and_loopback(self):
        os.environ["HARNESS_TOKEN"] = "t0ken-for-tests"
        try:
            server = serve(self.split, Config(), "127.0.0.1", 0, None)
        finally:
            del os.environ["HARNESS_TOKEN"]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/prepare"
        body = json.dumps(request()).encode()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST"), timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": "Bearer t0ken-for-tests", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(json.load(r)["contract_version"], CONTRACT)
        server.shutdown()
        with self.assertRaises(ValueError):
            serve(self.split, Config(), "0.0.0.0", 0, None)

    def test_team_dashboard_client_accepts_the_response(self):
        sys.path.insert(0, str(REPO / "dashboard"))
        try:
            import evaluation
        except ImportError as exc:  # certifi missing outside the project venv
            self.skipTest(f"dashboard/evaluation.py not importable here: {exc}")
        server = serve(self.split, Config(), "127.0.0.1", 0, None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = evaluation.validate_harness({"enabled": True, "url": f"http://127.0.0.1:{server.server_port}/prepare",
                                              "timeout_s": 30, "token": ""})
        source = evaluation.validate_input({"original_prompt": "What is my cat's name?", "question_id": "q_0_cat",
                                            "question_date": QD, "context": {"dataset_history_id": "h_0"}, "dry_run": True})
        prepared = evaluation.prepare(source, config, "run_" + "b" * 32)
        message, evidence = evaluation.compile_message(prepared, "1234abcd")
        self.assertLessEqual(len(message["text"]), 16000)
        self.assertIn("s_0_cat", {e["id"] for e in evidence})
        server.shutdown()


if __name__ == "__main__":
    unittest.main()

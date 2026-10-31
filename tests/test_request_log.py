"""Every client call the run makes is logged (sent, done) with ids and outcome only: never question text or the key."""
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts src/ on the path)
from memory_challenge.reactor.backends import ClientBackend

STUB = '''import json, sys
a = sys.argv
if "--new-history" in a:
    print(json.dumps({"status": "history", "history_id": "rh_1"}))
elif "--text" in a:
    print(json.dumps({"status": "reply", "reply": "OK", "history_id": a[a.index("--history") + 1]}))
else:
    print(json.dumps({"status": "refused", "code": "history_changed", "current_history_id": "rh_other",
                      "question_id": a[a.index("--question-id") + 1]}))
    sys.exit(4)
'''


class RequestLog(unittest.TestCase):
    def test_one_sent_and_one_done_line_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "client.py").write_text(STUB)
            (tmp / "ev.json").write_text("[]")
            log = tmp / "requests.jsonl"
            b = ClientBackend(tmp / "client.py", "http://127.0.0.1:9", "agt_x", 5, request_log=log)
            b.new_history()
            b.ask("rh_1", "What is the secret name of my cat?", "2023/05/30 (Tue) 08:08", tmp / "ev.json", "q_1", 16000)
            b.text("rh_1", "Reply only OK.")
            rows = [json.loads(l) for l in log.read_text().splitlines()]
            self.assertEqual([(r["seq"], r["event"], r["action"]) for r in rows],
                             [(1, "sent", "new-history"), (1, "done", "new-history"), (2, "sent", "question"),
                              (2, "done", "question"), (3, "sent", "text"), (3, "done", "text")])
            self.assertTrue(all(isinstance(r["pid"], int) for r in rows if r["event"] == "sent"))
            self.assertEqual(rows[1]["new_history_id"], "rh_1")
            self.assertEqual((rows[3]["question_id"], rows[3]["history_id"], rows[3]["code"], rows[3]["exit_code"],
                              rows[3]["current_history_id"]), ("q_1", "rh_1", "history_changed", 4, "rh_other"))
            self.assertNotIn("secret name", log.read_text())


if __name__ == "__main__":
    unittest.main()

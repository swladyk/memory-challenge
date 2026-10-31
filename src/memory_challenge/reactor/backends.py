"""Reactor backends with the official client's output shape.

``ClientBackend`` runs the unmodified ``reactor/client.py`` as a subprocess, exactly as the supplied driver does
(``reactor/examples/run_benchmark.py:49-55``): the access key reaches the client only through the environment it
inherits (``REACTOR_ACCESS_KEY``); this code never reads, logs or passes it. ``FakeBackend`` imitates the client's
JSON lines for tests and dry runs and contacts nothing.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from .message import INSUFFICIENT, official


class Backend(Protocol):
    agent_id: str

    def new_history(self) -> dict: ...

    def ask(self, history_id: str, question_text: str, question_date: str, evidence_path: Path, question_id: str,
            max_chars: int) -> dict: ...

    def text(self, history_id: str, text: str) -> dict: ...


class ClientBackend:
    """``request_log``: a JSONL file getting one line per client call (every request this process sends to Reactor):
    action, Reactor history, question id, client pid, start/end, outcome. Never the question, answer or key."""

    def __init__(self, client_path: Path, base_url: str, agent_id: str, timeout_s: float, python: str = sys.executable,
                 request_log: Path | None = None):
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1") \
                and not base_url.startswith("http://localhost"):
            raise ValueError("base URL must be https:// (reactor/client.py:153-161)")
        self.client_path, self.base_url, self.agent_id = Path(client_path), base_url, agent_id
        self.timeout_s, self.python = timeout_s, python
        self.request_log = Path(request_log) if request_log else None
        self._seq = 0

    def _log_request(self, record: dict) -> None:
        if self.request_log is None:
            return
        self.request_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.request_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _run(self, args: list[str]) -> dict:
        self._seq += 1
        action = "new-history" if "--new-history" in args else "text" if "--text" in args else "question"
        meta = {"seq": self._seq, "action": action, "agent_id": self.agent_id,
                "history_id": args[args.index("--history") + 1] if "--history" in args else None,
                "question_id": args[args.index("--question-id") + 1] if "--question-id" in args else None}
        started = time.time()
        out = self._call(args, lambda pid: self._log_request({**meta, "event": "sent", "pid": pid,
                                                              "ts": round(started, 3)}))
        self._log_request({**meta, "event": "done", "ts": round(time.time(), 3), "wall_s": round(time.time() - started, 3),
                           "status": out.get("status"), "code": out.get("code"), "exit_code": out.get("exit_code"),
                           "retries": out.get("retries"), "new_history_id": out.get("history_id") if action == "new-history" else None,
                           "current_history_id": out.get("current_history_id")})
        return out

    def _call(self, args: list[str], on_start) -> dict:
        cmd = [self.python, str(self.client_path), "--base-url", self.base_url, "--agent-id", self.agent_id,
               "--timeout", str(self.timeout_s), *args]
        env = os.environ.copy()
        if "SSL_CERT_FILE" not in env:  # as the team's reactor/smoke_test.py does; TLS verification stays on
            try:
                import certifi
                env["SSL_CERT_FILE"] = certifi.where()
            except ImportError:
                pass
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                env=env)
        on_start(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_s + 120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return {"status": "error", "error": "client process did not exit in time", "local_timeout": True}
        try:
            out = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
        except (ValueError, IndexError):
            out = None
        if not isinstance(out, dict):
            # The client never prints the key (reactor/PARTICIPANTS.md:9-10); keep only the tail of its stderr.
            out = {"status": "error", "error": (stderr or stdout or "no output").strip()[-500:]}
        out["exit_code"] = proc.returncode
        return out

    def new_history(self) -> dict:
        return self._run(["--new-history"])

    def ask(self, history_id, question_text, question_date, evidence_path, question_id, max_chars) -> dict:
        return self._run(["--history", history_id, "--question", question_text, "--question-date", question_date,
                          "--evidence", str(evidence_path), "--question-id", question_id, "--max-chars", str(max_chars)])

    def text(self, history_id, text) -> dict:
        """``--text``: a verbatim message (no template, no ref); returns the first reply after it, plus late replies."""
        return self._run(["--history", history_id, "--text", text])


_ABSTAINED = re.compile(rf"^\W*{INSUFFICIENT}\b", re.IGNORECASE)  # mirror of client.py's _INSUFFICIENT


def citations(answer: str, passages: list[dict]) -> tuple[list[dict], list[str]]:
    """The official client's ``citations()`` (``[E#]`` labels → the ids of the passages that were sent)."""
    mod = official()
    if mod is not None:
        return mod.citations(answer, passages)
    groups = re.findall(r"\[([^\[\]]*)\]", answer)
    cited = sorted({int(n) for g in groups for n in re.findall(r"\bE(\d+)\b", g)})
    known = [{"label": f"E{n}", "id": passages[n - 1]["id"]} for n in cited if 1 <= n <= len(passages)]
    return known, [f"E{n}" for n in cited if not 1 <= n <= len(passages)]


_citations = citations


def abstained(answer: str) -> bool:
    """The official client's INSUFFICIENT_EVIDENCE check."""
    mod = official()
    pattern = getattr(mod, "_INSUFFICIENT", None) if mod is not None else None
    return bool((pattern or _ABSTAINED).match(answer))


def passages_from(evidence_path: Path) -> list[dict]:
    """The passages exactly as the client loaded them for that question (``load_evidence``), so labels map the same."""
    raw = Path(evidence_path).read_text(encoding="utf-8")
    mod = official()
    if mod is not None and hasattr(mod, "load_evidence"):
        return mod.load_evidence(raw)
    return [{"id": e.get("id"), "text": str(e.get("text", "")).strip(), **({"date": e["date"]} if e.get("date") else {})}
            for e in json.loads(raw)]


def default_policy(question: str, passages: list[dict]) -> tuple[str | None, list[int]]:
    """A deterministic stand-in answerer: cites the passage sharing most words with the question and answers with
    its first quoted line. Not a model of Reactor's quality; it only exercises the protocol."""
    if not passages:
        return None, []
    qw = set(re.findall(r"[a-z]{4,}", question.lower()))
    best = max(range(len(passages)), key=lambda i: len(qw & set(re.findall(r"[a-z]{4,}", passages[i]["text"].lower()))))
    first = passages[best]["text"].split("\n")[0].split(": ", 1)[-1][:120]
    return first, [best + 1]


class FakeBackend:
    """Imitates reactor/client.py output lines. ``script`` maps question_id → outcomes to return in order before the
    policy answers (e.g. {"status": "timeout"}, {"status": "refused", "code": "busy", "forwarded": False}); an outcome
    may carry ``late_for`` (a question id whose earlier ref reappears as a late final answer) or ``late: True`` (a
    timeout whose policy answer arrives as a late final with the next request in the same history, as on Reactor).
    ``script["__ping__"]`` scripts ``text()`` the same way; ``hold: True`` keeps queued late answers back (not produced
    yet). A new history drops late answers still queued."""

    def __init__(self, agent_id: str = "agt_fake", policy: Callable = default_policy, script: dict | None = None,
                 seed: int = 0):
        self.agent_id, self.policy, self.script = agent_id, policy, {k: list(v) for k, v in (script or {}).items()}
        self.rng = random.Random(seed)
        self.histories = 0
        self.current_history: str | None = None
        self.calls: list[tuple[str, str]] = []
        self.refs: dict[str, str] = {}
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.queued_late: list[dict] = []
        self.cycle = 0

    def new_history(self) -> dict:
        self.histories += 1
        self.current_history = f"fake-{self.agent_id}-{self.histories}"
        self.queued_late = []
        self.calls.append(("new_history", self.current_history))
        return {"status": "history", "history_id": self.current_history, "running": True}

    def _drain_late(self) -> list[dict]:
        late, self.queued_late = self.queued_late, []
        return late

    def _final(self, question_text: str, passages: list[dict]) -> str:
        answer, labels = self.policy(question_text, passages)
        return INSUFFICIENT if answer is None else f"{answer} " + "".join(f"[E{n}]" for n in labels)

    def text(self, history_id, text) -> dict:
        self.calls.append(("text", text))
        self.cycle += 1
        base = {"history_id": history_id, "text": text, "other_replies": [], "late_replies": [], "retries": 0}
        if history_id != self.current_history:
            return {**base, "status": "refused", "code": "history_changed", "forwarded": False,
                    "current_history_id": self.current_history}
        queued = self.script.get("__ping__")
        if queued:
            outcome = dict(queued.pop(0))
            late_for, hold = outcome.pop("late_for", None), outcome.pop("hold", False)
            if late_for and late_for in self.refs:
                self.queued_late.append({"ref": self.refs[late_for], "answer": "a late answer", "cycle": self.cycle})
            return {**base, **outcome, "late_replies": [] if hold else self._drain_late()}
        return {**base, "status": "reply", "reply": "OK", "cycle": self.cycle, "latency_s": 0.0,
                "late_replies": self._drain_late()}

    def ask(self, history_id, question_text, question_date, evidence_path, question_id, max_chars) -> dict:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.calls.append(("ask", question_id))
            self.cycle += 1
            ref = f"{self.rng.getrandbits(32):08x}"
            self.refs[question_id] = ref
            base = {"question_id": question_id, "history_id": history_id, "ref": ref, "question": question_text,
                    "other_replies": [], "late_replies": [], "retries": 0}
            if history_id != self.current_history:
                return {**base, "status": "refused", "code": "history_changed", "forwarded": False,
                        "current_history_id": self.current_history}
            passages = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
            if len(question_text) + sum(len(p["text"]) for p in passages) > max_chars:
                return {**base, "status": "error", "error": f"the message is over --max-chars {max_chars}"}
            base["late_replies"] = self._drain_late()  # an earlier question's final, reported with this request
            queued = self.script.get(question_id)
            if queued:
                outcome = dict(queued.pop(0))
                late_for = outcome.pop("late_for", None)
                if late_for and late_for in self.refs:
                    base["late_replies"] = base["late_replies"] + [
                        {"ref": self.refs[late_for], "answer": "a late answer", "cycle": self.cycle}]
                if outcome.pop("late", False):  # Reactor keeps working after the client gives up
                    self.queued_late.append({"ref": ref, "answer": self._final(question_text, passages),
                                             "cycle": self.cycle + 1})
                return {**base, **outcome}
            final = self._final(question_text, passages)
            cited, unknown = citations(final, [{"id": p.get("id"), "text": p["text"]} for p in passages])
            return {**base, "status": "answered", "answer": final, "reply": f"# FINAL ANSWER [ref:{ref}]: {final}",
                    "cycle": self.cycle, "latency_s": 0.0, "citations": cited, "unknown_citations": unknown,
                    "uncited": bool(passages) and not cited, "abstained": final.startswith(INSUFFICIENT)}
        finally:
            with self._lock:
                self.in_flight -= 1

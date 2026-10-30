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
from pathlib import Path
from typing import Callable, Protocol

from .message import INSUFFICIENT, official


class Backend(Protocol):
    agent_id: str

    def new_history(self) -> dict: ...

    def ask(self, history_id: str, question_text: str, question_date: str, evidence_path: Path, question_id: str,
            max_chars: int) -> dict: ...


class ClientBackend:
    def __init__(self, client_path: Path, base_url: str, agent_id: str, timeout_s: float, python: str = sys.executable):
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1") \
                and not base_url.startswith("http://localhost"):
            raise ValueError("base URL must be https:// (reactor/client.py:153-161)")
        self.client_path, self.base_url, self.agent_id = Path(client_path), base_url, agent_id
        self.timeout_s, self.python = timeout_s, python

    def _run(self, args: list[str]) -> dict:
        cmd = [self.python, str(self.client_path), "--base-url", self.base_url, "--agent-id", self.agent_id,
               "--timeout", str(self.timeout_s), *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=os.environ.copy(),
                                  timeout=self.timeout_s + 120)
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "client process did not exit in time", "local_timeout": True}
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
        except (ValueError, IndexError):
            out = None
        if not isinstance(out, dict):
            # The client never prints the key (reactor/PARTICIPANTS.md:9-10); keep only the tail of its stderr.
            out = {"status": "error", "error": (proc.stderr or proc.stdout or "no output").strip()[-500:]}
        out["exit_code"] = proc.returncode
        return out

    def new_history(self) -> dict:
        return self._run(["--new-history"])

    def ask(self, history_id, question_text, question_date, evidence_path, question_id, max_chars) -> dict:
        return self._run(["--history", history_id, "--question", question_text, "--question-date", question_date,
                          "--evidence", str(evidence_path), "--question-id", question_id, "--max-chars", str(max_chars)])


def _citations(answer: str, passages: list[dict]) -> tuple[list[dict], list[str]]:
    mod = official()
    if mod is not None:
        return mod.citations(answer, passages)
    groups = re.findall(r"\[([^\[\]]*)\]", answer)
    cited = sorted({int(n) for g in groups for n in re.findall(r"\bE(\d+)\b", g)})
    known = [{"label": f"E{n}", "id": passages[n - 1]["id"]} for n in cited if 1 <= n <= len(passages)]
    return known, [f"E{n}" for n in cited if not 1 <= n <= len(passages)]


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
    may carry ``late_for`` (a question id whose earlier ref reappears as a late final answer)."""

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

    def new_history(self) -> dict:
        self.histories += 1
        self.current_history = f"fake-{self.agent_id}-{self.histories}"
        self.calls.append(("new_history", self.current_history))
        return {"status": "history", "history_id": self.current_history, "running": True}

    def ask(self, history_id, question_text, question_date, evidence_path, question_id, max_chars) -> dict:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.calls.append(("ask", question_id))
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
            queued = self.script.get(question_id)
            if queued:
                outcome = dict(queued.pop(0))
                late_for = outcome.pop("late_for", None)
                if late_for and late_for in self.refs:
                    base["late_replies"] = [{"ref": self.refs[late_for], "answer": "a late answer", "cycle": 1}]
                return {**base, **outcome}
            answer, labels = self.policy(question_text, passages)
            if answer is None:
                final = INSUFFICIENT
            else:
                final = f"{answer} " + "".join(f"[E{n}]" for n in labels)
            cited, unknown = _citations(final, [{"id": p.get("id"), "text": p["text"]} for p in passages])
            return {**base, "status": "answered", "answer": final, "reply": f"# FINAL ANSWER [ref:{ref}]: {final}",
                    "cycle": 1, "latency_s": 0.0, "citations": cited, "unknown_citations": unknown,
                    "uncited": bool(passages) and not cited, "abstained": final.startswith(INSUFFICIENT)}
        finally:
            with self._lock:
                self.in_flight -= 1

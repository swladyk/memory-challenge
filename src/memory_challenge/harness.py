"""The team dashboard's HTTP harness (contract ``halo.harness.v1``, ``dashboard/API.md``) backed by this retriever.

The dashboard POSTs ``{contract_version, run_id, question_id, original_prompt, question_date, evidence, context}``
to ``/prepare``. This server finds ``context.dataset_history_id`` in the configured split's ``histories.jsonl``,
retrieves and packs evidence for the prompt exactly as ``memret retrieve`` does, and returns:

- ``prompt``: the original prompt plus the fixed answering guidance and any computed date notes (``question_text``);
- ``question_date``: unchanged (the team's export rejects a changed date);
- ``evidence``: packed passages, ``id`` = session id of that history, ``date`` = that history's session date.

Incoming ``evidence`` is ignored: retrieval is this harness's job (use the workflow's ``--evidence-mode none``). A
request without ``context.dataset_history_id``, or with one that isn't in the split, gets HTTP 422 and the dashboard
fails closed (Reactor is not called). Messages fit the dashboard's 16,000-character check by construction (both use
the kit's ``build_message``). Each payload and trace is written under ``--out`` for ``memret why`` and for the
citation fallback. Loopback only; with ``HARNESS_TOKEN`` set, requests need ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .data.loader import SplitPaths, history_id_of, parse_history
from .evaluation.runner import payload_record, trace_record
from .indexes.history_index import HistoryIndex
from .reactor.message import mirror_parse_question_date
from .retrieval.controller import Retriever
from .schemas import Question

CONTRACT = "halo.harness.v1"
MAX_REQUEST = 1_000_000


class HarnessError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class HarnessApp:
    """Retrieval for harness requests over one split; history indexes cached (LRU) by dataset history id."""

    def __init__(self, split_dir: Path, cfg: Config, out_dir: Path | None = None, cache_size: int = 4):
        paths = SplitPaths(Path(split_dir))
        paths.check()
        self.cfg, self.out_dir, self.cache_size = cfg, out_dir, cache_size
        self.histories_path = paths.histories
        self.offsets: dict[str, tuple[int, int]] = {}
        with open(paths.histories, "rb") as f:
            pos = 0
            for raw in f:
                if raw.strip():
                    self.offsets[history_id_of(raw[:300].decode("utf-8", "replace"))] = (pos, len(raw))
                pos += len(raw)
        self.retriever = Retriever(cfg)
        self._cache: OrderedDict[str, HistoryIndex] = OrderedDict()
        self._lock = threading.Lock()

    def index(self, history_id: str) -> tuple[HistoryIndex, float]:
        with self._lock:
            if history_id in self._cache:
                self._cache.move_to_end(history_id)
                return self._cache[history_id], 0.0
        t0 = time.perf_counter()
        start, length = self.offsets[history_id]
        with open(self.histories_path, "rb") as f:
            f.seek(start)
            record = json.loads(f.read(length).decode("utf-8-sig"))
        idx = HistoryIndex(parse_history(record), self.cfg)
        built = time.perf_counter() - t0
        with self._lock:
            self._cache[history_id] = idx
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return idx, built

    def prepare(self, request: dict) -> dict:
        if request.get("contract_version") != CONTRACT:
            raise HarnessError(400, f"unsupported contract_version (expected {CONTRACT})")
        prompt = request.get("original_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HarnessError(400, "original_prompt required")
        qdate = request.get("question_date")
        if not isinstance(qdate, str) or not qdate:
            raise HarnessError(400, "question_date required")
        try:
            mirror_parse_question_date(qdate)
        except ValueError as exc:
            raise HarnessError(400, str(exc)) from None
        context = request.get("context") if isinstance(request.get("context"), dict) else {}
        hid = context.get("dataset_history_id")
        if not isinstance(hid, str) or not hid:
            raise HarnessError(422, "context.dataset_history_id is required: this harness retrieves from that dataset history")
        if hid not in self.offsets:
            raise HarnessError(422, f"dataset history {hid} is not in {self.histories_path}")
        qid = request.get("question_id") if isinstance(request.get("question_id"), str) and request.get("question_id") \
            else str(request.get("run_id") or "q_unknown")
        idx, built = self.index(hid)
        q = Question(qid, hid, prompt, qdate)
        t0 = time.perf_counter()
        res = self.retriever.run(q, idx)
        took = time.perf_counter() - t0
        if self.out_dir:
            for sub, rec in (("payloads", payload_record(q, res, self.cfg)), ("traces", trace_record(q, res, built, built > 0))):
                d = Path(self.out_dir) / sub
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{qid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"question_id": qid, "dataset_history_id": hid, "passages": len(res.pack.evidence),
                          "message_chars": res.pack.message_chars, "index_ms": round(built * 1000, 1),
                          "retrieval_ms": round(took * 1000, 1), "mode": res.mode}), file=sys.stderr, flush=True)
        return {"contract_version": CONTRACT, "prompt": res.pack.question_text, "question_date": qdate,
                "evidence": res.pack.evidence}


def make_handler(app: HarnessApp, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                return self._reply(200, {"contract_version": CONTRACT, "histories": len(app.offsets),
                                         "config_hash": app.cfg.hash(), "mode": app.cfg.mode()})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/prepare":
                return self._reply(404, {"error": "not found"})
            if token and not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self._reply(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST:
                    raise HarnessError(400, "invalid request size")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise HarnessError(400, "request must be a JSON object")
                self._reply(200, app.prepare(request))
            except HarnessError as exc:
                self._reply(exc.status, {"error": str(exc)})
            except json.JSONDecodeError:
                self._reply(400, {"error": "invalid JSON"})
            except Exception as exc:  # fail closed: the dashboard then does not call Reactor
                self._reply(500, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

    return Handler


def serve(split_dir: Path, cfg: Config, host: str = "127.0.0.1", port: int = 8890, out_dir: Path | None = None,
          ready: threading.Event | None = None) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the harness serves conversation data: bind it to loopback only")
    app = HarnessApp(split_dir, cfg, out_dir)
    server = ThreadingHTTPServer((host, port), make_handler(app, os.environ.get("HARNESS_TOKEN") or None))
    if ready is not None:
        ready.set()
    return server

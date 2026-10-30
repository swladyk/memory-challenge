"""Synthetic histories for tests. Answer keys used by tests live in the tests, never in production code."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_challenge.config import Config  # noqa: E402
from memory_challenge.data.loader import parse_history  # noqa: E402
from memory_challenge.indexes.history_index import HistoryIndex  # noqa: E402
from memory_challenge.schemas import Question  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FILLER = [
    ("Can you explain how photosynthesis works?", "Photosynthesis converts light energy into chemical energy in plants."),
    ("What are good stretches after running?", "Try hamstring, quad and calf stretches, holding each for 30 seconds."),
    ("Give me a quick pasta recipe.", "Boil pasta, toss with garlic, olive oil, chili flakes and parmesan."),
    ("How do I write a cover letter?", "Open with the role, show one achievement, and close with a call to action."),
]


def session(sid: str, date: str, turns: list[tuple[str, str]]) -> dict:
    return {"session_id": sid, "date": date,
            "turns": [{"turn_id": f"t_{sid}_{i}", "role": r, "content": c} for i, (r, c) in enumerate(turns)]}


def filler(prefix: str, date: str, n: int = 4) -> list[dict]:
    out = []
    for i in range(n):
        u, a = FILLER[i % len(FILLER)]
        out.append(session(f"s_{prefix}f{i}", date, [("user", u), ("assistant", a)]))
    return out


def history(hid: str, sessions: list[dict]) -> dict:
    return {"history_id": hid, "sessions": sessions}


def index_of(record: dict, cfg: Config | None = None) -> HistoryIndex:
    return HistoryIndex(parse_history(record), cfg or Config())


def question(qid: str, hid: str, text: str, date: str = "2023/05/30 (Tue) 08:08") -> Question:
    return Question(qid, hid, text, date)


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path

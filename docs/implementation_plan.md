# Implementation plan

For the MVP in `docs/architecture_proposal.md` §3. Nothing here is built yet; this phase stops at planning.
The kit files (`reactor/client.py`, `reactor/predictions.py`, `score.py`, `convert_longmemeval.py`) stay untouched;
our code calls them as subprocesses, the way `reactor/examples/run_benchmark.py:49-55` does, so organisers re-running
our code exercise the same client.

## 1. Layout (additions only)

```
memory-challenge/
├── memret/                      # our package
│   ├── data.py                  # load splits, dataclasses, content hashing, HistoryKey, fingerprints
│   ├── units.py                 # turns → index units (context prefixes, long-turn windows)
│   ├── lexical.py               # tokenizer + BM25 (from analysis/retrieval_baselines.py)
│   ├── dense.py                 # E3, optional
│   ├── fuse.py                  # RRF
│   ├── rerank.py                # E5, optional
│   ├── temporal.py              # date parsing, relative-date resolver, days-before annotations
│   ├── excerpt.py               # query-focused trimming, neighbour expansion
│   ├── select.py                # coverage-aware, budget-aware packing
│   ├── payload.py               # evidence JSON + question text; budget mirror of client.py:218-246
│   ├── runner.py                # Reactor driver around client.py (history lifecycle, resume, logs)
│   ├── validate.py              # leakage whitelist, citations ⊂ own history, one line per question
│   └── evaluate.py              # retrieval metrics; score.py via subprocess
├── scripts/
│   ├── build_payloads.py        # questions + histories → runs/<run>/payloads/*.json + manifest.json
│   ├── eval_retrieval.py        # payloads (or rankings) + gold → metrics tables
│   ├── run_reactor.py           # payloads → attempts.jsonl, results.jsonl
│   └── finalize.py              # validate → reactor/predictions.py → score.py (dev)
├── configs/mvp.toml             # every parameter of a run; its hash goes into the manifest
├── tests/                       # pytest
├── analysis/                    # exists: exploration scripts and outputs
└── docs/                        # exists: these documents
```

`runs/` and payloads hold conversation text: gitignored, never published. Dependencies: numpy, scipy, scikit-learn,
nltk (already present) for the MVP; `websockets>=14` for Reactor; sentence-transformers only if E3/E5 are approved.
Pin them in a `requirements.txt` when the first one is added.

## 2. Interfaces

```python
HistoryKey = tuple[str, str]                      # (data_fingerprint, history_id)

@dataclass(frozen=True)
class SessionRef:
    key: HistoryKey; session_id: str; date_str: str; date: datetime; position: int

@dataclass(frozen=True)
class Unit:
    unit_id: str; key: HistoryKey; session_id: str; turn_id: str; role: str
    char_start: int; char_end: int; index_text: str; display_text: str

@dataclass
class Excerpt:
    key: HistoryKey; session_id: str; date_str: str; turn_ids: list[str]
    spans: list[tuple[str, int, int]]; text: str; score: float
    annotations: list[tuple[str, str, str]]       # (kind, surface, value)

class HistoryIndex:                               # one per HistoryKey, never shared
    def __init__(self, history: dict, cfg: Config): ...
    def search(self, query: str, k: int) -> list[tuple[Unit, float]]: ...

def retrieve(question: dict, index: HistoryIndex, cfg: Config) -> list[tuple[Unit, float]]: ...
def make_excerpts(question: dict, hits, index: HistoryIndex, cfg: Config) -> list[Excerpt]: ...
def select(excerpts: list[Excerpt], budget: Budget, cfg: Config) -> list[Excerpt]: ...
def build_payload(question: dict, selected: list[Excerpt], cfg: Config) -> dict: ...   # sees no gold
def run_reactor(payload_dir: Path, out_dir: Path, agent: AgentConfig) -> None: ...      # key from env only
```

Example payload (dev-large knowledge-update case, abbreviated; written to `runs/<run>/payloads/<question_id>.json`):

```json
{
  "question_id": "q_6863ec3c6b22",
  "history_key": ["sha256:9c1e…", "h_…"],
  "question_date": "2023/06/25 (Sun) 13:22",
  "question_text": "What was my personal best time in the charity 5K run?\n\nGuidance: if a value changed over time, the most recent statement is current. Cite every passage you use as [E#]. Answer with the value only.",
  "evidence": [
    {"id": "s_b86c55fffa33", "date": "2023/05/23 (Tue) 13:01",
     "text": "[33 days before the question]\nuser: … I recently set a personal best time in a charity 5K run with a time of 27:12. …"},
    {"id": "s_66386059c5b0", "date": "2023/05/30 (Tue) 13:53",
     "text": "[26 days before the question]\nuser: I'm training for another charity 5K run … beat my personal best time of 25:50 this time around."}
  ],
  "budget": {"limit": 16000, "template": 515, "question": 196, "headers": 122, "passage_text": 248, "total": 1081},
  "provenance": [{"label": "E1", "session_id": "s_b86c55fffa33", "turn_ids": ["t_…"], "spans": [["t_…", 0, 214]]},
                 {"label": "E2", "session_id": "s_66386059c5b0", "turn_ids": ["t_…"], "spans": [["t_…", 0, 196]]}],
  "config_hash": "…"
}
```

The runner writes `evidence` to a temp file and calls
`client.py --history H --question <question_text> --question-date <question_date> --evidence <file> --question-id Q
--max-chars <limit>`. Result lines are the client's own JSON, unchanged.

## 3. Runner semantics

- Per dataset history: `--new-history`, then its questions in the configured order, one at a time.
- Status handling: `answered` → final; `refused` with retryable codes is already retried by the client; `timeout` /
  `closed` → re-ask once in the same Reactor history after the client's backoff window; if the history was replaced
  (`history_changed`) or the run restarts, start a new history and ask the remaining questions.
- `attempts.jsonl` gets every client line; `results.jsonl` is rewritten atomically with exactly one line per question
  (the last final status), so `predictions.py` never sees duplicates (`reactor/predictions.py:78-84`).
- Resume: questions with a final `answered` line are skipped; timeouts/errors are not (unlike
  `run_benchmark.py:73`).
- The access key is read by `client.py` from the environment; our code never reads, logs or passes it.

## 4. Tests

| Area | Cases |
| --- | --- |
| Data | dev and dev-large share history ids → different `HistoryKey`s; a session reused in two histories keeps each history's own date; turn order and roles preserved |
| Units | short user reply gets the preceding assistant tail as context; a 40k-char user turn splits into windows with correct char spans; both roles indexed |
| BM25 | hand-computed scores on a 3-document corpus; empty query → zeros; ranking stable |
| Temporal resolver | table of (session date, phrase, expected): Sat 2023-05-20 + "yesterday" → 2023-05-19; + "last Friday" → 2023-05-19; + "two weeks ago" → 2023-05-06; + "next Monday" → 2023-05-22; + "on May 5th" → 2023-05-05; Wed 2023-01-04 + "December 28th" → 2022-12-28; unresolvable phrase left untouched |
| Excerpts | spans map back to the exact original substring; speaker labels always present; no excerpt exceeds its cap |
| Selection | total message length by our formula ≤ limit for random inputs; per-session cap respected; chronological output |
| Budget mirror | our formula equals `len(build_message(...))` from `reactor/client.py` for 50 random payloads (imports the kit read-only) |
| Leakage | payload builder raises if any gold key appears; payload ids ⊂ the question's own history; dates equal this history's dates |
| Runner (mocked client) | one result line per question after retries; resume skips only final statuses; a new Reactor history per dataset history; never two histories interleaved |
| Finalize | citations outside the question's history are reported and removed before `predictions.py`; `score.py` runs on dev |
| Regression | retrieval metrics on a fixed 50-question dev-large sample not below the MVP's recorded values |

## 5. Milestones

Estimates are focused engineering hours for one person, with a wide uncertainty band; Reactor wall time is listed
separately because it is set by the agent, not by us.

| # | Milestone | Depends on | Estimate | Done when |
| --- | --- | --- | --- | --- |
| M0 | Package skeleton, config, data loader, keys, fingerprints | — | 2–3 h | data tests pass |
| M1 | BM25 turn index + retrieval eval harness (port from `analysis/`) | M0 | 2–4 h | reproduces E1 numbers (±0.005) |
| M2 | Units with context, excerpting, coverage-aware packing, payload builder, validation (E2) | M1 | 5–8 h | E2 success criterion met or explicitly rejected |
| M3 | Temporal resolver + annotations | M0 | 3–5 h | resolver tests + ≥ 0.95 precision on the 100-phrase hand set |
| M4 | Reactor runner + finalize pipeline (needs `websockets`, team URL/agent id/key) | M0 | 3–5 h | 5-question smoke run end to end, predictions validated and scored |
| M5 | MVP E2E on the 90-question subset | M2, M3, M4 | 2–3 h analysis + Reactor wall time (~0.5–1.5 h, unmeasured) | per-type table + error review |
| M6 | Dense + RRF + rerank (E3–E5; needs model approval) | M1 | 4–8 h | experiments decided |
| M7 | Second retrieval round + bounded re-ask (E7) | M2 (+M5 for re-ask) | 3–6 h | experiment decided |
| M8 | Temporal claim layer (E9; optional) | M3, M5 error review | 8–16 h | experiment decided |
| M9 | Freeze config, lockbox, test run (146 questions), README for submission | M5 (+ accepted M6–M8) | 3–4 h + Reactor wall time | `predictions.jsonl` validated; code + README reproduce it |

**Critical path**: M0 → M1 → M2 → M4 → M5 → M9 (≈ 17–27 h of work plus two Reactor runs).
M3 and M6 run in parallel with M2; M7/M8 only after M5's error review.

## 6. Parallel work across teammates

| Person | Owns | Interface they deliver |
| --- | --- | --- |
| A — retrieval | M1, M6, M7 (retrieval part) | `HistoryIndex.search`, eval tables |
| B — evidence & time | M2 excerpting/selection, M3 | `make_excerpts`, `select`, `temporal.annotate` |
| C — Reactor ops | M4, runner, finalize, logging, reproducibility README | `run_reactor`, `finalize` |
| D — analysis & story | error taxonomy from M5, abstention calibration, presentation | per-type failure reports |

Shared contract from M0: the dataclasses, `Config`, and the payload JSON schema above. Any change to them needs all
owners' sign-off because cache keys and tests depend on them.

## 7. MVP cut line and fallback

**MVP (must ship)**: M0–M5 + M9 — BM25 turn index with context, query-focused excerpts, coverage-aware 16k packing,
temporal annotations, our runner and validation.

**Fallback (if Reactor access or time is short)**: the measured E1 configuration — BM25 turn ranking packed greedily into
14,000 characters of whole turns cut at 3,000 characters (`analysis/retrieval_baselines.py:pack`, pack14k recall 0.902
on dev-large vs the kit's 0.563) — through the same runner. It needs only M0, M1, M4.

**Not in the MVP**: dense, rerank, claim layer, second retrieval round, 32k budget, whole-history loading — each enters
only through its experiment.

## 8. Approvals and inputs needed before implementation

1. Go-ahead to implement the MVP (M0–M3 need no network and no new dependencies).
2. Install `websockets>=14` and run Reactor: the team's base URL, agent id(s), and the access key entered by you at a
   hidden prompt in your own terminal (`reactor/PARTICIPANTS.md:28-36`); approval of Reactor usage — a 5-question
   smoke test, then ~90 calls per subset run, ~146 for test.
3. Optional model downloads for E3/E5: `sentence-transformers` plus one small embedder (e.g. a bge/e5/gte small or base
   model, ~100–500 MB) and one cross-encoder (~90 MB). Exact models chosen at that step.
4. Organiser answers to `docs/repository_audit.md` §10 (abstaining without Reactor, notes in the question text,
   late replies, server limit, number of agents, score combination).
5. A permanent home for this work: the repository currently sits in a temporary session folder.

## 9. Decision brief

1. **What the data and code show.** Reactor answers, our code retrieves; abstention and citations reach the submission
   only through Reactor's `INSUFFICIENT_EVIDENCE` sentinel and `[E#]` labels. Whole evidence sessions do not fit the
   16k message for most KU/MS/TR questions. Lexical BM25 over turns already finds single-session and update evidence on
   dev-large (all@10 ≥ 0.97) and, at a 14k budget, nearly doubles the kit baseline's complete-evidence rate (0.831 vs
   0.446); the gaps are multi-session completeness
   (all@10 0.69), temporal (0.71), and precision among candidates (R@1 ≤ 0.66 outside SSA). Test differs from dev: one
   persona per history with ~10 questions, ~44 short persona sessions among ~155 reused filler sessions, a 6-month span,
   and relative dates in 74% of the new sessions' user turns vs ~25–28% in dev.
2. **Recommendation.** Lexical-first, excerpt-centric retrieval with deterministic temporal annotations and our own
   Reactor runner; dense retrieval and reranking enter only if they pass their experiments; a query-time temporal claim
   layer (no graph database) is the optional advanced extension.
3. **Why not the alternatives first.** Dense/rerank: needs downloads and lexical recall is saturated on 3 of 5 types —
   measure first. Hierarchies and summaries: information loss and many LLM calls for a budget problem that excerpting
   addresses. Memory frameworks: LLM-heavy ingestion that is either off-machine (not allowed) or fragile locally, no
   published results at our scale, and their answerers are replaced by Reactor anyway.
4. **Highest-risk assumptions.** That test evidence behaves like dev evidence despite the structural differences; that
   Reactor follows the sentinel and citation instructions reliably; that within-history memory does not hurt later
   questions; that the organisers accept guidance text in the question; and that the server limit and latency allow the
   planned runs.
5. **Next tasks.** M0 → M1 → M2 (E2) and M3 in parallel; then the Reactor smoke test and the 90-question subset.
6. **Needed from you.** The approvals and inputs in §8.

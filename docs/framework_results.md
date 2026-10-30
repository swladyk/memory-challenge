# Framework results

Every number here was **measured** on this machine from this branch's code and the release `data-v1` files. Nothing
here comes from a live Reactor call: none has been made. Anything not measured is marked "not run" or "proposed".
How to reproduce: `docs/framework.md`.

## Environment

| | |
| --- | --- |
| Machine | Apple M2 Pro, 10 CPU cores, 16 GB RAM, macOS 26.6.2 |
| Python | 3.12.7 (Anaconda), core uses the standard library only |
| Branch | `feat/retrieval-framework` on top of `f0a317d`, uncommitted |
| Message builder | the kit's own `build_message` from `reactor/client.py` (`message_builder: official` in the dry-run and test-split manifests) |
| Optional packages | `sentence-transformers` not installed; no model downloaded |

## Status by component

| Component | Implemented | Tested | Measured | Notes |
| --- | --- | --- | --- | --- |
| Loader, validation, fingerprints | yes | yes | yes (`inspect-data`: dev and test, 0 errors) | |
| Context-preserving windows (lists, ordinals, lead-ins, short replies, exact spans) | yes | yes | yes | |
| Dates and conservative relative-date notes | yes | yes | indirectly (payloads) | resolver precision on real text not hand-checked yet |
| BM25 + per-component search + RRF + strength gating | yes | yes | yes | |
| One recovery round for uncovered components | yes | yes | yes (ablation) | |
| Structural graph + bounded expansion | yes | yes | yes (ablation) | |
| Personalized PageRank source | yes | via ablation run only | yes (ablation) | local, HippoRAG-inspired; not a HippoRAG 2 reproduction |
| Dense retrieval, cross-encoder rerank | yes | missing-dependency path tested | **not run** | needs the `.[dense]` extra and ~220 MB of models |
| Packing to the exact client message length | yes | yes | yes (dev-large, test) | |
| Reactor scheduler, checkpoints, resume, retries | yes | yes, fake backend | dry run with the fake backend | **live path not run** |
| Prediction export (kit converter) + strict validation + official scorer | yes | yes | dry run | |
| Supported-citation fallback (organisers, 2026-10-30) | yes | yes (fake backend + kit converter) | no | needs real Reactor answers |
| Claims/events, UPDATES/CONTRADICTS edges, summaries, compression | no | — | — | proposed |

## Tests

`python3 -m pytest -q` and `python3 -m unittest discover -s tests -t tests`: **38 passed** (under 1 s), with no
credentials and no network. Behaviours covered (`tests/`):

- **Citation fallback and retries (`test_citation_fallback.py`)**: distinctive answer units (a lone single digit
  never counts); a supported answer gets its passage cited after the kit conversion, with the answer text unchanged;
  an unsupported answer stays uncited (no top-ranked fallback); a passage from another history is never cited even
  when it matches; computed notes are not evidence; abstentions and existing citations are untouched; every
  decision is logged; `previous_pending` is retried up to 3 more times (7 s apart by default).

- **Data (`test_data.py`)**: list items keep their ordinal and the list's lead-in line; bullets get positions; a short
  reply carries the assistant turn it answers; every span recovers its original text exactly; overlapping windows
  merge without duplicated text; long paragraphs split at sentences; dataset dates with weekday checks;
  relative-date table (yesterday, tomorrow, last Friday, two weeks ago, next Monday, May 5th, today; a December
  date said in January resolves to the previous year; unresolvable text left alone); question time ranges;
  malformed and empty inputs.
- **Message (`test_message.py`)**: the mirrored template equals the kit's `build_message` on 50 random payloads ×
  3 question-date formats.
- **Retrieval (`test_retrieval.py`)**: the kit's wedding example (both sessions, chronological, exact dates); a hall
  whose name is never given (relevant sessions retrieved, every quoted piece is original text, coverage labelled
  heuristic); an assistant-only recommendation found without an assistant cue; "the seventh tip" returns item 7
  with its lead-in; a three-session aggregation packs all three sessions; relative dates and day differences with
  inputs and sources; a later unrelated event does not overwrite an earlier one; a session reused in two histories
  keeps each history's date and resolves "yesterday" against it; a candidate from another history is rejected;
  message length equals the official builder's and never exceeds 2,500 / 6,000 / 16,000; prompt-injection text
  stays inside its passage and never changes the plan or question text; a missing optional model fails with an
  install instruction; empty history and empty question; kit-baseline mode.
- **Reactor harness (`test_reactor.py`, fake backend)**: one Reactor history per dataset history, file order,
  never more than one question in flight per agent, one result line per question; a refusal that never reached the
  agent is retried, a timeout is not (unless asked); a late final answer is logged and never taken as current;
  resume skips final answers and starts a new Reactor history for what's left; payloads from two configurations
  are refused; `E#` labels map per question; export through the kit's `predictions.py` (Reactor abstention →
  `abstain: true`, timeout → reported missing, unknown label dropped) and strict validation (foreign-history
  citations and duplicate ids rejected), with U+2028 inside the fixture text.

## Retrieval on dev-large (tune partition)

Partition: deterministic 70/30 split by history id (`evaluation/runner.py:partition_of`), so dev/dev-large pairs and
cued/uncued twins stay together. Tune = 363 questions in 324 histories (≈473 sessions each); lockbox = 163 questions
in 146 histories, **not evaluated yet**.

### First measurement (1,200-character windows, floor on fused scores)

Before the strength gate was added (packing floor relative to the top fused score): complete evidence packed (`pack_all`) 0.846; multi-session 0.699; temporal
0.711; answer text in pack 0.482; packed precision 0.266; mean message 13.5k characters. Retrieval latency per
question: warm p50 12 ms, cold (first question of a history) p50 71 ms; index build per history p50 702 ms
(p95 1,977 ms). Rank fusion alone could not gate packing (scores of rank 1 and rank 100 differ by less than 3×),
which led to the per-source strength (raw score ÷ top raw score) used since.

### Parameter sweep that set the defaults

Seven settings on 150 tune histories. **This subset was biased**: it took the first 150 histories in file order,
which contain no temporal questions (the files are ordered by type). `--limit` now samples evenly.

| window chars | windows/session | strength floor | pack_all | MS pack_all | answer text (all / SSU / SSA / KU) | precision | chars | passages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1,200 | 3 | 0.30 | 0.961 | 0.850 | 0.636 / 0.735 / 0.643 / 0.688 | 0.306 | 11,989 | 7.87 |
| 2,000 | 3 | 0.30 | 0.967 | 0.875 | 0.653 / 0.735 / 0.679 / 0.688 | 0.318 | 12,532 | 7.13 |
| 3,000 | 3 | 0.30 | 0.961 | 0.850 | 0.653 / 0.765 / 0.679 / 0.688 | 0.322 | 12,797 | 6.79 |
| 2,000 | 4 | 0.30 | 0.967 | 0.875 | 0.670 / 0.794 / 0.679 / 0.688 | 0.323 | 12,897 | 6.96 |
| 2,000 | 3 | 0.20 | 0.967 | 0.875 | 0.653 / 0.735 / 0.679 / 0.688 | 0.182 | 14,724 | 9.02 |
| 2,000 | 3 | 0.45 | 0.956 | 0.850 | 0.636 / 0.735 / 0.679 / 0.625 | 0.546 | 9,076 | 4.53 |
| 3,000 | 2 | 0.30 | 0.961 | 0.850 | 0.610 / 0.706 / 0.625 / 0.688 | 0.314 | 12,331 | 7.10 |

Defaults chosen: 2,000-character windows, 4 windows per session, strength floor 0.4 (between the precision of 0.45 and
the recall of 0.3). The full ablation below re-measures the floor on all types.

### Ablation (all 363 tune questions, 344 answerable)

`run-ablation` (command in `docs/framework.md`), same questions for every config, 8 worker processes; output
`runs/ablation-dev-large-tune/`. Answerable questions per type: SSU 40, SSA 78, MS 83, KU 53, TR 90. **Noise scale:
one question moves "all" by 0.003, MS by 0.012, TR by 0.011, KU by 0.019.**

`pack_all` = every gold session is among the packed passages; precision = share of packed sessions that are gold;
answer text = the short gold answer string appears verbatim in the packed text (SSU 34, SSA 56, MS 28, KU 35, TR 71
questions have such an answer; MS/TR answers are mostly computed, so they rarely appear verbatim).

| Config | Mode | pack_all (all) | SSU | SSA | MS | KU | TR | packed recall | precision | answer text (all) | mean / max chars | passages |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a-kit | kit keyword | 0.465 | 0.675 | 0.910 | 0.169 | 0.547 | 0.211 | 0.577 | 0.355 | 0.344 | 8,517 / 13,007 | 4.0 |
| b-lexical-windows | lexical | 0.831 | 0.975 | 1.000 | 0.687 | 0.962 | 0.678 | 0.909 | 0.407 | 0.509 | 12,593 / 16,000 | 6.6 |
| **baseline** | lexical+recovery | **0.834** | 0.975 | 1.000 | 0.699 | 0.962 | 0.678 | 0.910 | 0.407 | 0.509 | 12,593 / 16,000 | 6.6 |
| b2-attribute-expansion | lexical+recovery | 0.843 | 0.975 | 1.000 | 0.711 | 0.981 | 0.689 | 0.918 | 0.399 | 0.509 | 12,693 / 16,000 | 6.8 |
| g-no-time-boost | lexical+recovery | 0.834 | 0.975 | 1.000 | 0.699 | 0.962 | 0.678 | 0.910 | 0.407 | 0.509 | 12,593 / 16,000 | 6.6 |
| f-graph | lexical+graph+recovery | 0.834 | 0.975 | 1.000 | 0.699 | 0.962 | 0.678 | 0.910 | 0.407 | 0.509 | 12,640 / 16,000 | 6.6 |
| h-ppr | lexical+graph+ppr+recovery | 0.834 | 0.975 | 1.000 | 0.699 | 0.962 | 0.678 | 0.910 | 0.407 | 0.509 | 12,640 / 16,000 | 6.6 |
| p-floor-030 | lexical+recovery, floor 0.3 | 0.840 | 0.975 | 1.000 | 0.711 | 0.981 | 0.678 | 0.913 | 0.296 | 0.509 | 14,147 / 16,000 | 7.9 |
| p-budget-32k | lexical+recovery, 32k | 0.861 | 0.975 | 1.000 | 0.735 | 0.962 | 0.744 | 0.927 | 0.373 | 0.522 | 19,980 / 31,999 | 9.9 |
| hybrid | dense+rerank+graph | **not run**: `OptionalDependencyError` (sentence-transformers not installed; no download without approval) | | | | | | | | | | |

Candidate ranking before packing (baseline): all gold sessions in the top 10 sessions for 0.843 of questions, top 50
for 0.927 (kit: 0.465).

Latency per question on dev-large (≈4.8M characters and ≈10k windows per history), single process per history:

| Config | Index build p50 / p95 | Cold retrieval p50 (first question, incl. packing) | Warm retrieval p50 | Packing p50 / p95 | Wall time, 363 questions, 8 workers |
| --- | --- | --- | --- | --- | --- |
| a-kit | 689 / 1,702 ms | 152 ms | 128 ms | — | 69.0 s |
| baseline | 587 / 1,805 ms | 57 ms | 4.7 ms | 49 / 106 ms | 46.7 s |
| f-graph | 718 / 1,823 ms | 621 ms (graph built on first use) | 4.6 ms | 50 / 129 ms | 103.6 s |
| h-ppr | 593 / 1,558 ms | 2,956 ms | 7.8 ms | 52 / 100 ms | 144.2 s |
| p-budget-32k | 542 / 1,499 ms | 73 ms | 4.0 ms | 49 / 250 ms | 41.8 s |

Most dev-large histories have one question, so the overall retrieval p50 is close to the cold figure. Index build
times vary by a few hundred ms between runs of identical code (586–650 ms p50 for configs that build the same index).
Reactor latency is not included anywhere: it has not been measured.

### What the ablation shows

- **Windows + BM25 + strength-gated packing is the step change**: complete evidence 0.465 → 0.831 over the kit's
  example retriever, multi-session 0.169 → 0.687, temporal 0.211 → 0.678, with higher precision (0.355 → 0.407).
- **The recovery round, attribute expansion and a lower floor each move results by one to three questions**: within
  noise individually. Attribute expansion went the same way in MS, KU and TR (+1 question each) at −0.009
  precision. It's a candidate, not a confirmed gain; re-check it on the lockbox before adopting.
- **The time-range boost changed nothing** on dev-large tune: few questions name a range the planner resolves, and
  the boost reorders but never changed which sessions were packed.
- **Graph expansion and PPR changed no packed session**, and cost 0.6 s and 3.0 s cold latency per history. Why,
  from the configuration: an expanded window gets its parent's strength × `decay^depth`. NEXT/PREV neighbours (depth 1,
  × 0.5) can clear the 0.4 floor only next to very strong windows, and they stay in the same session: they added
  ~47 characters per message on average, not sessions. Cross-session hops go through an entity (depth 2, × 0.25),
  so their strength is at most 0.25 and they can never be packed; only list items reached by position (factor 1.0)
  escape this, and dev-large has few such questions. PPR-only windows get half their relative PPR score and rarely
  clear the floor either. Graph features stay off by default; their unit-tested purpose (ordinal list items) is not
  exercised by this data. Under the experiment plan's criteria they're rejected for now (no gain, latency over
  target); a retest needs a strength rule for expanded windows that isn't tied to the lexical floor.
- **The 32k budget is the largest single lever**: complete evidence +0.027 overall, temporal +0.067 (6 questions),
  multi-session +0.036 (3 questions), at ~20k characters per message and precision 0.373. It needs the organisers to
  confirm the server limit, and longer messages cost Reactor time (`reactor/PARTICIPANTS.md:122`), which is
  unmeasured.
- **Precision is the floor's trade-off**: 0.407 at floor 0.4 vs 0.296 at 0.3 for two more complete questions and
  +1.5k characters. Whether Reactor cites better with fewer distractors can only be measured end to end.

## End-to-end dry run (fake Reactor, dev)

20 dev histories (22 questions), `configs/baseline.toml`:

1. `retrieve` wrote 22 payloads and traces.
2. `run-reactor --backend fake`: 22 answered, one result line per question; `--backend live` without `--allow-live`
   refused.
3. `export-predictions`: the kit's `predictions.py` (unmodified, through the session-id adapter) wrote 22
   predictions, exit 0, 0 skipped.
4. `validate-predictions`: valid (no foreign-history citations, no duplicates); 504 dev questions outside the subset
   reported missing, as expected; the official `score.py` ran on the file.

The fake backend quotes a line of its best passage and cites it, so the scored accuracy (0.002) and evidence F1
(0.031 over all 526 questions) only show that the plumbing works; they say nothing about Reactor.

## Test split payloads (retrieval only, no labels)

`retrieve --split test --config configs/baseline.toml`: 146 payloads in 10 s (2 workers). 0 over the 16,000-character
limit (max 15,993); 0 passages from another history; 0 dates that differ from the history's own date; message
median 9,530 characters; passages per question median 12 and max 12, i.e. **at the `max_sessions` cap**. Test
histories mix ~44 short persona sessions into reused filler, and many of them clear the strength floor for a given
question (`docs/eda_deep_dive.md` §3: test rankings are 2–3× more crowded than dev-large's). Packed precision on test
is therefore likely lower than on dev-large; it cannot be measured without labels.

## Kit converter issue found

`reactor/predictions.py` reads `--histories` and the results file with `str.splitlines()`
(`reactor/predictions.py:159`, `:167`), which also splits on U+2028. U+2028 occurs inside conversation text in every
released split: dev 97, dev-large 577, test 8 times. Run as documented (`--histories test/histories.jsonl`), the
converter exits 2 ("Unterminated string") and writes nothing. Workaround in `reactor/predictions.py` of this package
(`export()`): pass the kit a session-id-only histories file, and write `results.jsonl` ASCII-escaped as the client
does. Reported as open question 7 in `docs/repository_audit.md`.

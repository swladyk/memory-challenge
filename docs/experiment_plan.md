# Experiment plan

Retrieval-only experiments first (free, minutes); Reactor experiments only after approval, on a fixed stratified subset;
the full dev-large run and the test run last.

## 1. Evaluation hygiene

**Leakage rules (enforced in code, `memret/validate.py`)**
- Payloads are built from `questions.jsonl` + `histories.jsonl` only. A payload schema whitelist rejects any field
  from `gold.jsonl` (answer, aliases, evidence, question_type, abstain).
- Gold is read only by `evaluate.py`, after rankings/payloads are frozen to disk.
- No lookups, mappings or features derived from the public LongMemEval files (no id reversal via the default salt,
  `convert_longmemeval.py:138`).
- No features from cross-split structure: content overlap with dev/dev-large, session "novelty", or session length as a
  relevance prior (test construction artifact, `docs/data_profile.md` §4).
- A shared session id never carries dates or other per-history state across histories.
- Test has no labels; nothing is tuned on test outputs. Test is run once per frozen configuration.

**Partitions.** dev and dev-large are two difficulty conditions over the same 526 questions and evidence sets; they are
not independent. We define **question groups** = a question, its uncued twin if any (56 pairs), and its dev/dev-large
versions. Groups are split once, stratified by gold type and abstention, into:
- **tune** (70%, ~330 questions) — used for every decision;
- **lockbox** (30%, ~160 questions) — evaluated only at milestone gates, never for choosing parameters.
Rule-based components have few parameters; the lockbox is there to catch overfitting to tune, not to claim
generalisation to test, which differs structurally (data_profile §9). Report that limitation with every number.

**Primary condition**: dev-large (closest haystack scale). dev is reported as the easy condition.

**Metrics** (per question type, plus overall; never only the average):

| Stage | Metric |
| --- | --- |
| Retrieval | session recall@k (k = 1, 5, 10, 20, 50) and all@k; pack recall / all-gold / precision under 14k and 30k; answer-text-in-pack for matchable answers; uncued-SSA subset separately |
| Payload | passages, distinct sessions, characters (passage text, notes, total message by the client template formula) |
| Reactor (E2E) | `score.py` correct / unjudged / abstention recall / false abstention / evidence F1; plus our own: cited-session precision and recall, `uncited` rate, unknown-label rate, sentinel compliance (abstentions phrased other than `INSUFFICIENT_EVIDENCE`), timeout/closed rate |
| Cost | index build time and memory per history; retrieval p50/p95 per question (cold = first query after load, warm = subsequent); Reactor latency p50/p95 (`latency_s`); retries; end-to-end wall time |

Unjudged answers on the E2E subset are reviewed by hand against gold (diagnostic only; predictions are never edited).
Retrieval scores alone never classify a question as unanswerable in reporting.

## 2. Ablation sequence

Every experiment changes one thing relative to the current best, on the tune partition of dev-large (dev reported
alongside). "Reject" means the change stays out of the pipeline.

### E0 — Supplied baseline (done, retrieval-only)
- **Hypothesis**: the kit's keyword retrieval is a weak floor.
- **Result [measured]**: dev-large recall 0.563, all-gold 0.446 (MS 0.149, TR 0.213); dev 0.736 / 0.609.
- **E0b (Reactor, optional, needs approval)**: kit baseline end-to-end on the E2E subset to calibrate how retrieval
  metrics translate into answers; optionally whole-history loading on 2–3 dev-large histories to measure its cost.

### E1 — Lexical units (done, retrieval-only)
- **Result [measured]**: BM25 turn > round ≈ session at equal budget; turn pack14k recall 0.902, all 0.831; user-only
  units pack more sessions but lose SSA answer text (0.178 vs 0.756). Turn units are the base.

### E2 — Context windows, long-turn splitting, query-focused excerpts
- **Hypothesis**: trimming assistant turns to query-matching sentences and adding context to short user turns raises
  pack all-gold for MS/TR at the same budget without losing SSA answer text.
- **Change**: unit construction and excerpting only.
- **Metrics**: pack14k all-gold and recall per type; answer-text-in-pack; passages per payload.
- **Cost**: CPU seconds.
- **Success**: MS and TR pack14k all-gold +0.05 absolute over E1 (0.653 / 0.709), SSA answer text ≥ 0.75, SSU ≥ 0.80.
- **Reject if**: answer-text-in-pack drops > 0.03 on any type, or gains < 0.02 (noise level for ~90–130 questions).

### E3 — Dense retrieval (needs approval: sentence-transformers + a small embedder)
- **Hypothesis**: dense retrieval finds paraphrased evidence BM25 misses, mainly MS/TR and uncued SSA.
- **Change**: dense top-200 alone, then as a second candidate source.
- **Metrics**: recall@10/50 and all@10/50 per type; union recall (BM25 ∪ dense); embedding time per history and memory.
- **Cost**: one-off embedding of dev-large (~2.3M turns; distinct content ~48.8k sessions) — time to be measured; test
  (30k turns) small.
- **Success**: union all@50 for MS/TR +0.05 over BM25 alone.
- **Reject if**: union gain < 0.02 or embedding the test split takes > 30 min on this machine.

### E4 — Hybrid fusion
- **Hypothesis**: RRF of BM25 and dense beats either alone on pack all-gold.
- **Change**: RRF (k = 60), then a weighted variant.
- **Success**: pack14k all-gold +0.03 over the best single retriever on MS/TR with no type losing > 0.02.
- **Reject if**: no type improves by ≥ 0.02.

### E5 — Reranking (needs approval: a cross-encoder)
- **Hypothesis**: reranking top-50 excerpts raises R@1/R@5 and packed precision (→ evidence F1) while keeping recall.
- **Metrics**: R@1, R@5, pack precision, pack all-gold; rerank latency p50/p95 per question.
- **Success**: pack precision +0.05 and R@1 +0.05 with pack all-gold within −0.01; p95 latency < 3 s.
- **Reject if**: all-gold drops > 0.02 or latency target missed.

### E6 — Temporal/update annotations and payload ordering
- **Hypothesis**: resolved dates, days-before-question, chronological ordering and the guidance block improve KU and TR
  answers. Retrieval metrics cannot show this; it needs Reactor.
- **Change**: payload formatting only, same excerpts. A/B on the E2E subset (two Reactor runs).
- **Metrics**: correct + unjudged-reviewed on KU/TR; evidence F1; sentinel compliance.
- **Offline check first**: resolver unit-test accuracy on a hand-labelled set of 100 relative expressions sampled from
  dev user turns (precision ≥ 0.95 required before any Reactor run).
- **Success**: KU+TR correct +5 points absolute on the subset (≈ 36 questions; report the interval).
- **Reject if**: no improvement, or any other type loses > 3 points.

### E7 — Multi-session coverage and bounded recovery
- **Hypothesis**: a second, novelty-constrained retrieval round for aggregation/temporal questions raises MS/TR
  all-gold; a single bounded re-ask fixes some wrong abstentions.
- **Change**: second round (retrieval-only first); re-ask policy (Reactor).
- **Metrics**: MS/TR pack all-gold; abstention recall, false abstention; extra Reactor calls.
- **Success**: MS/TR pack all-gold +0.05; false abstention −30% relative with abstention recall unchanged; extra
  calls < 15% of questions.
- **Reject if**: abstention recall falls, or extra calls > 20%.

### E8 — Budget: 16k vs 32k
- **Hypothesis**: 32k raises MS/TR coverage enough to beat its cost in latency and distraction.
- **Metrics**: correct, evidence F1, Reactor latency p50/p95 at both budgets on the E2E subset.
- **Reject 32k if**: accuracy gain < 3 points or evidence precision falls by > 0.05.

### E9 — Graph/claim layer, hierarchy, or learned compression (only if justified)
- **Precondition**: E2–E8 leave a specific failure class (e.g. KU latest-value errors with both values retrieved; MS
  counts wrong with all items retrieved) of ≥ 10% of a type.
- **Hypothesis**: the C-lite claim layer fixes that class.
- **Success**: the targeted class's errors −50% on the subset, no other type −3 points, added p95 latency < 10 s.
- **Reject if**: extraction precision < 0.9 on a hand-checked sample, or the gain is within noise.
- Learned compression only if E8 shows budget-bound losses after E2's extractive trimming.

### E10 — In-history contamination (Reactor, optional)
- **Hypothesis**: earlier questions' evidence and answers in a Reactor history change later answers.
- **Design**: dev has at most 2 questions per history, so the only in-rules probe is the 56 cued/uncued pairs (same
  history, same gold). On 10 pairs, compare the uncued question (a) asked first vs (b) asked after its cued twin, in
  separate runs, each with one Reactor history per dataset history. (b) − (a) is an upper bound on direct answer
  leakage. The realistic test case (10 different questions about one persona) has no dev analogue within the rules;
  we accept that risk and rely on self-contained payloads. Small and descriptive; informs the question-ordering policy.

## 3. End-to-end subset

Fixed before any Reactor call, from the **tune** partition of dev-large, stratified, seeded:

| Type | Answerable | Abstain |
| --- | --- | --- |
| single-session-user | 14 | 2 |
| single-session-assistant | 16 (8 uncued; twins excluded) | 0 |
| multi-session | 18 | 4 |
| knowledge-update | 14 | 2 |
| temporal-reasoning | 18 | 2 |
| **total** | 80 | 10 → **90** |

Reactor cost: 90 calls per configuration. At "tens of seconds" per answer [repo wording, unmeasured], roughly 0.5–1.5 h
of serial agent time per run; two to four runs during development. The full dev-large run (526 calls) only for the
final configuration, if time allows; the lockbox E2E slice (~40 questions) once, at the end.

## 4. Order of work and stopping rules

1. E2 (retrieval-only) → freeze MVP retrieval.
2. With approval: E3–E5 (retrieval-only).
3. With Reactor access: E0b smoke (5 questions), then the subset run of the MVP; E6, E8.
4. E7 and E10 if time; E9 only on a demonstrated failure class.
5. Lockbox, then the test run with the frozen configuration.

Stop adding components when the next experiment's success criterion is not met twice in a row, or when less than one
full test run plus a margin of time remains before the deadline.

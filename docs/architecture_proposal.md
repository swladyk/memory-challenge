# Architecture proposal

Inputs: `docs/repository_audit.md` (rules, kit behaviour) and `docs/data_profile.md` (measurements). Labels:
**[measured]**, **[repo]**, **[hypothesis]** (to test, see `docs/experiment_plan.md`), **[unverified]** (third-party
claim we could not check).

## 1. What the evidence says the system must do

| Finding | Source | Design consequence |
| --- | --- | --- |
| Whole evidence exceeds 16k chars for 72/72 KU, 116/121 MS, 114/127 TR questions; >32k for 14/61/47 | [measured] data_profile §7 | The unit sent to Reactor is a **turn-level excerpt**, never a session |
| Assistant turns are 87% of text, 9× longer than user turns; SSA answers live in assistant turns (66/70) | [measured] §2, §7 | Index both roles; spend budget on **query-focused trimming of assistant turns** |
| BM25 over turns: single-session/KU all@10 ≥ 0.97; MS 0.69, TR 0.71 on dev-large | [measured] §8 | The retrieval gap is **multi-session completeness**, not first-hit recall |
| R@1 0.36–0.66 outside SSA; packed precision 0.25–0.34 | [measured] §8 | **Choosing among candidates** (rerank, citation precision) drives evidence F1 |
| Lexical near-misses common (dev-large: 30/56 SSU, 32/54 KU answers also in other sessions) | [measured] §7 | Need attribute/entity discrimination, not just term overlap |
| 74% of new test sessions have relative dates in user turns (dev ~27%); test spans 179 days | [measured] §4–§5 | **Deterministic relative-date resolution** is first-class, not an extension |
| Test: one persona, ~10 questions per history, "now/current" cues 2× dev | [measured] §6, §9 | Updates and near-misses within one life story; in-history memory effects |
| Lexical abstention signal weak (AUC 0.77, n=30) | [measured] §8 | Abstention stays Reactor's sentinel decision, informed by evidence quality |
| Local BM25: 0.5 s to index a 4.8M-char history, 0.5 ms per query | [measured] §8 | Local compute is negligible; **Reactor calls are the critical path** |
| Reactor: one question at a time per agent, tens of seconds per answer, final answer only from Reactor | [repo] audit §3 | One Reactor call per question by default; all retrieval offline, before the run |

## 2. Options compared

Assessments are [hypothesis] unless they cite a measurement or source. "Calls" means model calls (local or remote)
besides the single Reactor call per question.

| | A. Strong lexical | B. Hybrid lexical + dense + rerank | C. Hybrid + temporal claim/event graph | D. Hierarchical / map-reduce | E. Memory frameworks (Hindsight, Graphiti/Zep, HippoRAG 2) |
| --- | --- | --- | --- | --- | --- |
| **Expected gain by type** | Baseline measured: strong on SSU/SSA/KU, weak on MS/TR completeness | Paraphrase recall for MS/TR and uncued SSA; rerank lifts R@1 → citation precision | KU (latest value), TR (event dates, arithmetic), MS (dedup items) | MS aggregation via summaries; little for SSU/SSA | Depends on framework; none publishes LongMemEval_M results [subagent research] |
| **Integration** | Small: ported from `analysis/` | Medium: model download, fusion, rerank cut-offs | Medium–high: extraction + resolution + notes; graph optional | High: summaries per session/episode + drill-down | High: separate services, schemas, LLM ingestion |
| **Indexing / models** | None | Embedder (~100–500 MB) + cross-encoder (~90 MB) on M2/MPS; needs approval | Deterministic date resolver; optional small local LLM for claims (GBs) | LLM per session for summaries (3,000 test sessions; 222k dev-large) | Hindsight: LLM per chunk + Postgres/pgvector; Graphiti: several LLM calls per episode + Neo4j/FalkorDB; HippoRAG 2: 2 LLM calls per passage [subagent research] |
| **Query latency / calls** | ~1 ms [measured] | ~0.1–1 s rerank of top-50 [hypothesis, unmeasured] | + extraction over top candidates: seconds–minutes per question with a local LLM [hypothesis] | Query-time map-reduce: N LLM calls per question; ingestion-time: huge offline cost | Framework-dependent; Hindsight runs 4 retrievers + rerank per query |
| **Citation traceability** | Exact (session, turn range) | Exact | Exact if every claim keeps (session, turn, span) | Summaries need explicit back-links; drill-down adds a step | Hindsight: fact → document/chunk; Graphiti: edge → episodes; HippoRAG: passage ids [subagent research] |
| **Temporal correctness** | Only what the text and session date say | Same as A | Explicit: recorded vs event time, validity, latest-wins | Summaries blur dates unless designed in | Graphiti bi-temporal edges; Hindsight occurrence/mention times; HippoRAG none |
| **Multi-session completeness** | Weak (MS all@10 0.69) [measured] | Better recall [hypothesis] | Better aggregation if claims are complete | Designed for it | Unknown for our setting |
| **Abstention** | Reactor sentinel; weak lexical signal | Rerank scores as a better signal [hypothesis] | "No claim about X for this user" is explicit | Summaries may hallucinate coverage | Framework answerers bypass Reactor; we would only use retrieval |
| **Failure modes** | Vocabulary mismatch; near-misses ranked high | Dense near-misses (topically similar sessions); calibration | Extraction errors become confident wrong notes; small-model JSON failures | Information loss; LongMemEval paper: summaries/facts as values hurt QA except MS [subagent research] | Data would leave the machine with hosted LLMs (not allowed); small local models break Graphiti ingestion per its README [subagent research]; Zep's own LongMemEval_S table shows SSA dropping 94.6 → 80.4 [self-reported] |
| **Hackathon feasibility** | High | High if downloads approved | Medium (deterministic part high; LLM part medium) | Low–medium | Low |

## 3. Recommendation

### MVP: lexical-first, excerpt-centric retrieval with deterministic temporal annotations (A+, B-ready)

A strong lexical core plus the things the data shows are needed, with dense retrieval and reranking as **gated**
upgrades that enter only if they pass their experiments (E3–E5). Components:

1. **Turn index with context** — one unit per turn (user and assistant), long turns (> 2,500 chars) split into
   ~1,200-char windows on paragraph/sentence boundaries; the index text of a short user turn (< 40 chars) is prefixed
   with the tail of the preceding assistant turn; assistant units carry the preceding user prompt as context.
2. **BM25** (k1 1.2, b 0.75, Porter stemming, stop words), per history; session score = best unit (measured
   default) — alternatives (sum of top-2) tested in E2.
3. **Query-focused excerpting** — keep user turns whole up to ~1,200 chars; trim assistant turns to the sentences or
   list/table rows that match the query (plus the line before for context), ~400–900 chars; always keep speaker
   labels; neighbour expansion: a retrieved short user reply pulls its preceding assistant turn and vice versa.
4. **Coverage-aware packing** under the actual message budget (`budget = limit − 515 − len(question+notes) − 61 ×
   passages`), greedy by score × session-novelty, a per-session cap (≤ 3 excerpts), passages emitted in chronological
   order with their session date.
5. **Deterministic temporal annotations** — each excerpt's session date plus "N days before the question"; relative
   expressions resolved against the session date and appended inline (`last Friday [= Fri 2023-05-19]`); question
   date passed with `--question-date` as given.
6. **Question text** = the original question + a short, fixed guidance block: use the most recent statement when values
   change; cite every passage used with `[E#]`; answer with the value only; the `INSUFFICIENT_EVIDENCE` rule is
   already in the template (`reactor/client.py:243-244`). (Pending organiser confirmation, audit §10 Q2.)
7. **Reactor driver** — our own, calling `client.py` as a subprocess: one Reactor history per dataset history,
   questions in a fixed order, exactly one final result line per question, all attempts logged separately.
8. **Validation** before `predictions.py`: citations ⊂ the question's own history, schema, one line per question.

Why this first: it attacks the measured failure modes (budget, assistant turns, MS completeness, dates) with
deterministic, debuggable parts; it keeps every passage as original text with exact provenance; it needs no model
downloads; and it is cheap enough that all 526 dev-large questions can be evaluated retrieval-only in under a minute.

### Optional advanced extension: query-time temporal claim layer (C-lite, no graph database)

For questions whose top candidates contain changing values, dated events or countable items (detected from the
retrieved excerpts and question cues, not from gold types), extract **attributed claims** from the top ~20 candidate
excerpts only:

`claim = (subject, attribute, value, polarity, event_time?, recorded_time = session date, source = (session_id,
turn_id, char span), extractor, confidence)`

- Deterministic first: dates, durations, counts, "X → Y" updates via patterns; optional small local instruct model
  (needs approval) for attribute/value pairs.
- Store per question in memory or SQLite; group by (subject, attribute); latest recorded value wins for "now/current";
  compute day differences and counts in code.
- Output: **notes in the question text** that reference passage labels ("E2 (2023-05-23) says 27:12; E5 (2023-05-30)
  says 25:50 — later"), while the original excerpts remain the cited evidence.
- Bounded associative expansion: entities/values found in the claims seed one extra retrieval round for missing
  multi-session items (stop when budget full, no new session above threshold, or 2 rounds).

A graph database is unnecessary: per-question claim sets are tens of rows; adjacency in a dict is enough.

### Why the strongest alternatives are not first

- **B (dense + rerank) as the base**: plausible gains, but it needs model downloads (approval), and on our data lexical
  recall is already saturated for 3 of 5 types; we measure its marginal value on MS/TR before committing. It is the
  first upgrade in line, not rejected.
- **D (hierarchy / RAPTOR / map-reduce)**: the LongMemEval paper reports summaries/facts as stored values lose
  information and hurt QA except for multi-session [subagent research]; our test budget pressure is probably lower than
  dev-large's (short persona sessions, data_profile §9); ingestion-time summaries for 3,000+ sessions need an LLM we do
  not have approved; query-time map-reduce multiplies model calls on the critical path.
- **E (frameworks)**: their answer generation is replaced by Reactor anyway, so only their retrieval would remain —
  essentially option B plus LLM-extracted memories. Their ingestion needs an LLM per chunk/episode/passage; with hosted
  LLMs that uploads conversation data (not allowed), with local small models Graphiti warns of broken ingestion; no
  LongMemEval_M results are published for any of them; Zep's own table shows a large SSA drop [self-reported].
  Hindsight's retrieval recipe (BM25 + dense + RRF + cross-encoder + temporal channel) is informative and maps onto B.
- **Learned compression** (e.g. LLMLingua-style token pruning): the interface allows any evidence text, but pruned text
  can drop exactly the dates and numbers we need and weakens verbatim citation; extractive, query-focused trimming comes
  first, learned compression only if E9 shows a budget-bound loss after evidence quality is established.
- **Loading whole histories into Reactor**: allowed and "the baseline to beat" [repo], but a test history is ~1.65M
  characters [measured] ≈ 52 parts at 32k, each a full agent turn [repo]; at tens of seconds per turn that is tens of
  minutes per history [estimate from README wording, unmeasured], and citations still come only from `--evidence`.
  Worth one measured comparison on a few dev-large histories if Reactor time allows (E0b), not as the main path.

## 4. Data model

```
HistoryKey      = (data_fingerprint, history_id)          # history ids repeat across splits [measured]
SessionContent  : session_id, content_hash, turns[]        # shared, content-addressed, date-free
SessionRef      : HistoryKey, session_id, date_str, date (datetime), position
Turn            : turn_id, session_id, index, role ∈ {user, assistant}, content
Unit            : unit_id, HistoryKey, session_id, turn_id, char_start, char_end, role,
                  index_text (with context prefix), display_text
Excerpt         : HistoryKey, session_id, date_str, turn_ids[], spans[(turn_id, start, end)], text,
                  annotations[(kind, span, value)]   # e.g. ("resolved_date", "last Friday", "2023-05-19")
Claim (ext.)    : subject, attribute, value, polarity, event_time?, recorded_time, source span, extractor, confidence
Payload         : question_id, HistoryKey, question_text, question_date, passages[{id, date, text}],
                  budget {limit, overhead, used}, provenance {excerpt ids}, config_hash
```

- Dates are **never** stored on shared session content; they live on `SessionRef`, keyed by history.
- Caches: tokenisation and embeddings keyed by `(content_hash, unit_rule_version, model_id)` — safe to share across
  histories because content is identical by definition; BM25 statistics and rankings keyed by
  `(HistoryKey, index_version)` — never shared across histories. Any change to unit rules, tokeniser or model bumps the
  version and invalidates dependent caches.
- `data_fingerprint` = sha256 of `histories.jsonl` (dev and dev-large share history ids [measured]).

## 5. Retrieval and selection details

- **Candidates**: BM25 top-200 units (+ dense top-200 if E3 passes), fused with RRF (k = 60) if hybrid.
- **Rerank** (if E5 passes): cross-encoder over the top-50 *excerpts* (not whole turns).
- **Conditional second round** when the question has aggregation/temporal cues or the first round covers only one
  session strongly: pseudo-relevance feedback from the top excerpts' salient nouns/entities, retrieve again with a
  session-novelty constraint. Stop at budget, at no new session above a score floor, or after 2 rounds.
- **Selection objective**: maximise Σ score over selected excerpts with a bonus for the first excerpt from each new
  session and a penalty for > 3 from one session; fill until the budget is used or the marginal score falls below a
  floor. Fewer passages for single-fact questions improves citation precision [hypothesis, E6].
- **Budget**: default 16,000 (client default); 32,000 tested in E8 (README warns longer messages cost agent time).

## 6. Temporal reasoning

- **Recorded time** = session date in this history; **event time** = resolved from text when present, else unknown.
- Resolver: "today/yesterday/tomorrow", "last/this/next <weekday|week|weekend|month>", "N days/weeks/months ago",
  "on <weekday>", "<Month> <day>" (year = most recent past occurrence relative to the session date unless the text says
  otherwise). Unresolvable phrases stay as written. Resolved values are annotations; the original words remain.
- **Updates vs disagreements**: same (subject, attribute) at different recorded times → later one is current
  (`README.md:106`); same recorded day with conflicting values → present both, no rule.
- **Arithmetic**: in the MVP, per-excerpt "N days before the question" and resolved absolute dates; in the extension,
  differences and counts computed from claims, with the passage labels they come from.

## 7. Evidence and citations

- Passages are original excerpts only; notes are separate and labelled as computed.
- One passage per excerpt; several passages may share a session id; `predictions.py` de-duplicates session ids
  (`reactor/predictions.py:120-121`).
- To avoid merely topical citations: fewer distractor passages, chronological order with dates, explicit request to cite
  only passages that state the answer. Measured in E6 via evidence precision/recall on dev.

## 8. Abstention

- Decision by Reactor's sentinel (only path the kit supports without organiser confirmation, audit §10 Q1).
- We supply the best evidence we have; an empty or very weak retrieval still goes to Reactor with whatever was found.
- **Bounded recovery** (E7): if Reactor abstains on a question whose retrieval looked strong, or answers when retrieval
  looked weak, one second ask with a broader payload (32k, second round). Cost: one extra Reactor call for a minority of
  questions; risk: the first attempt remains in the agent's memory.
- Calibrate on dev only with question groups held out (experiment plan §1); report abstention recall and false
  abstention per type. A low retrieval score alone is not treated as proof of absence.

## 9. State and isolation

- One Reactor history per dataset history; questions in a fixed order (file order; on dev, uncued variant before its
  cued twin); never reset between questions; an interrupted history is restarted and its *remaining* questions asked
  (each carries its own evidence; `reactor/QUICKSTART.md:71-76`).
- Generated answers never flow back into indexes, payloads or caches.
- Results: `attempts.jsonl` (every client line), `results.jsonl` (exactly one final line per question),
  `payloads/<question_id>.json`, `manifest.json` (git sha, config hash, data fingerprints, timestamps).

## 10. Performance budget (targets, not measurements, unless marked)

| Stage | Where | Target | Notes |
| --- | --- | --- | --- |
| Index build per history | offline | < 2 s (BM25 measured 0.5 s p50 on dev-large) | test histories are 1/3 of dev-large size |
| Retrieval + excerpting + packing per question | offline | p95 < 1 s lexical; < 3 s with rerank | precomputed for all questions before any Reactor call |
| Reactor per question | online, serial per agent | not ours to set; "tens of seconds" [repo] | measure p50/p95 on the E2E subset |
| End-to-end test run | | ≈ 146 × Reactor latency (+ retries) | parallel only across agent ids |

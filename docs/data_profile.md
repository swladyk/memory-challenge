# Data profile

All numbers are **measured** on release `data-v1` (dev, dev-large, test) by the scripts in `analysis/`, unless marked
otherwise. Gold is used only for the diagnostics in §7–§8; no gold feeds anything that could reach inference.

Reproduce (Python 3.12, numpy/scipy/scikit-learn/nltk; no network; repo code is not imported):

```bash
python -I analysis/profile_data.py --root . --out analysis/out/data_profile.json        # ~2.3 min, 1 core
python -I analysis/retrieval_baselines.py --split dev --workers 8                       # ~6 s
python -I analysis/retrieval_baselines.py --split dev-large --workers 8                 # ~45 s
python -I analysis/question_cues.py --root .
python -I analysis/test_structure.py --root .
python -I analysis/inspect_cases.py --split dev --out analysis/out/cases.md
```

Profiling streams one history per line; dev-large took 104 s on one core of an M2 Pro (16 GB).

## 1. Integrity

Zero issues on every check in all three splits: duplicate history/question/turn ids, duplicate session ids within a
history, malformed fields, unparseable or out-of-order session dates, sessions dated after their question, questions
without a history or histories without a question, gold/question id mismatches, gold evidence ids missing from the
question's history, and the same session id carrying different content. Minor irregularities:

| | dev | dev-large | test |
| --- | --- | --- | --- |
| Empty turns (user / assistant) | 9 / 3 | 232 / 37 | 1 / 0 |
| Consecutive turns with the same role | 32 | 379 | 1 |
| Sessions whose first turn is the assistant's | 1,833 (8.2%) | 18,111 (8.1%) | 274 (9.1%) |
| Identical session content under several session ids | 588 contents | 1,731 contents | 4 contents |

## 2. Structure and size

| | dev | dev-large | test |
| --- | --- | --- | --- |
| Histories | 470 | 470 | 15 |
| Questions | 526 | 526 | 146 |
| Questions per history | 1.1 (1–2) | 1.1 (1–2) | 9.7 (9–10) |
| Sessions per history, p50 (min–max) | 48 (38–62) | 473 (458–487) | 200 (200–200) |
| Sessions in total / distinct ids / distinct contents | 22,428 / 18,299 / 17,628 | 222,321 / 51,405 / 48,800 | 3,000 / 3,000 / 2,996 |
| Turns user / assistant | 115,034 / 116,843 | 1,135,909 / 1,153,736 | 14,975 / 15,152 |
| Share of characters from user turns | 12.6% | 12.5% | 14.0% |
| Characters per history, p50 | 489k | 4.81M | 1.63M |
| History span (first session → question), days p50 (p90) | 10.7 (93.5) | 11.0 (93.6) | 178.8 (179.9) |
| Last session → question, days p50 (max) | 0.1 (187.8) | 0.0 (187.4) | 0.9 (3.7) |
| Question length, characters mean (p50) | 84 (72) | 84 (72) | 68 (69) |

Every test history has a single question date, always at `00:00`; no dev question date is at `00:00`.

### Characters per unit

| Unit | Split | p50 | p90 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- |
| User turn | dev | 184 | 369 | 456 | 1,269 | 76,560 |
| | dev-large | 183 | 368 | 454 | 1,236 | 76,560 |
| | test | 169 | 340 | 416 | 988 | 42,844 |
| Assistant turn | dev | 1,701 | 2,862 | 3,124 | 3,615 | 6,961 |
| | dev-large | 1,693 | 2,858 | 3,124 | 3,628 | 6,961 |
| | test | 1,373 | 2,733 | 3,030 | 3,568 | 4,348 |
| Session | dev | 10,395 | 16,884 | 18,177 | 20,578 | 78,117 |
| | dev-large | 10,294 | 16,867 | 18,244 | 20,616 | 78,117 |
| | test | 7,687 | 16,093 | 17,795 | 20,383 | 43,485 |

Sessions have 12 turns at the median (max 132 in dev, 34 in test).

| Count over threshold | dev | dev-large | test |
| --- | --- | --- | --- |
| Turns > 2,000 / 5,000 / 10,000 / 16,000 / 32,000 chars | 47,032 / 330 / 110 / 12 / 4 | 459,145 / 3,192 / 1,111 / 129 / 30 | 4,788 / 34 / 14 / 3 / 1 |
| Sessions > 10,000 / 16,000 / 32,000 chars | 11,806 / 3,261 / 4 | 115,367 / 32,118 / 30 | 1,215 / 313 / 1 |

The few turns over 16k are user turns with pasted material (all examples checked were turn 0 of their session).

No tokenizer is installed, so sizes are in characters only; no token counts are claimed.

**Chunking implications.** The client's whole message is limited to 16,000 characters (32,000 at the server), with
515 characters of fixed template plus 61 per passage (`docs/repository_audit.md` §4). A median session (~10k) already
takes two-thirds of the default budget, and 14% of sessions exceed it alone, so the unit we send cannot be the session.
Assistant turns (p50 ~1.7k, p99 ~3.6k) are about 9× longer than user turns (p50 ~180) and carry 87% of all text, so
trimming assistant turns is where budget is won or lost. The rare very long user turns (>5k) need splitting into
windows for indexing and excerpting.

## 3. Session reuse and dates

- Within dev, 3,517 of 18,299 session ids appear in more than one history (max 6); within dev-large, 46,232 of 51,405
  (p50 4 histories, max 20). **Every** reused session carries a different date in each history it appears in; the
  spread between a reused session's dates is p50 43 days (dev) and 99 days (dev-large), max 980 days.
- dev ↔ dev-large: the same 470 history ids and 526 question ids/texts, but no dev history is a subset of its dev-large
  namesake. Within the same history id, 1,116 sessions are shared and all of them carry a different date. Question dates
  differ for 524 of 526 questions (96 by calendar day). Gold evidence *sets* are identical (order differs for 114).
  **History ids are not unique keys across splits**: any cache must be keyed by (split or file hash, history_id).
- Test shares no session ids with dev or dev-large, but **2,330 of its 3,000 session contents occur verbatim in
  dev-large** (900 in dev) under other ids.

## 4. Test-split structure (inputs only; no labels exist)

`analysis/test_structure.py`: in each test history, 41–50 sessions (666 in total, 44.4 per history) have content that
appears in neither dev split. They are spread across the whole timeline (median position 0.33–0.67 of the history,
first at day 3–39, last at day 141–179) and are short: median 1.2–1.5k characters, against 9.7–11.3k for the reused
sessions of the same history. [inferred] These are the persona-specific sessions that will contain evidence and the
announced near-misses (`README.md:32`); the reused ones are LongMemEval filler.

**This is a construction artifact, not a property of user memory.** Novelty with respect to the public benchmark and
session length must not be used as relevance features or filters: they would not generalise and would amount to
exploiting how the test set was assembled. We record it because it changes how representative dev is (§9).

## 5. Content markers (heuristic regexes; indicative only)

Counted once per distinct session content, in the split where the content first appears (dev first, then new dev-large
contents, then new test contents). Patterns are in `analysis/profile_data.py:MARKERS`.

| Sessions with ≥1 marker in a user turn | dev (17,628) | dev-large, new (31,486) | test, new (666) |
| --- | --- | --- | --- |
| Relative date ("yesterday", "last week", "3 days ago", "on Friday") | 4,848 (27.5%) | 7,731 (24.6%) | 493 (74.0%) |
| Explicit date ("May 5th", "2/5/2023") | 1,259 (7.1%) | 1,710 (5.4%) | 122 (18.3%) |
| Update marker ("actually", "no longer", "switched to", "now I") | 4,240 (24.1%) | 6,560 (20.8%) | 230 (34.5%) |

Short user turns (< 40 characters, not the first turn), which usually need the preceding assistant turn to be
understood: 4,444 in dev, 44,447 in dev-large, 424 in test. Example: in a knowledge-update evidence session the user
answers "She moved to Chicago." to an assistant question about which city (`analysis/out/cases.md`).

## 6. Question phrasing, dev types vs test (`analysis/question_cues.py`)

Share of questions matching each cue pattern:

| | n | assistant reference | count / total | duration | order / when | current / latest | date anchor |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev, all | 526 | 0.103 | 0.433 | 0.213 | 0.213 | 0.067 | 0.110 |
| dev, knowledge-update | 78 | 0.000 | 0.474 | 0.077 | 0.179 | 0.244 | 0.000 |
| dev, multi-session | 133 | 0.000 | 0.835 | 0.143 | 0.098 | 0.053 | 0.090 |
| dev, single-session-assistant | 112 | 0.482 | 0.089 | 0.018 | 0.080 | 0.000 | 0.000 |
| dev, single-session-user | 70 | 0.000 | 0.200 | 0.129 | 0.057 | 0.029 | 0.057 |
| dev, temporal-reasoning | 133 | 0.000 | 0.421 | 0.571 | 0.541 | 0.053 | 0.316 |
| **test** | 146 | 0.144 | 0.473 | 0.247 | 0.178 | 0.130 | 0.021 |

Test questions are shorter, almost never anchor a date ("last March"), and use "now/current" about twice as often as
dev. All ~10 questions of a test history concern one coherent persona (e.g. a freelancer in Lisbon: cat's name,
coworking space the assistant recommended, ceramics instructor *now*, accountant *now*, days between two family events).
Cue-based type estimates are rough and are not used as labels.

## 7. Gold evidence (dev; types, abstention and evidence sets are identical in dev-large)

| Type | n | Abstain | Evidence sessions per answerable question | Evidence chars, p50 | Evidence > 16k / > 32k | Answer string found in evidence (user / both / assistant) | Evidence with a user relative date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| knowledge-update | 78 | 6 | 2 (all 72) | 27.5k | 72 / 14 | 46 of 54 matchable (24 / 20 / 2) | 39 / 72 |
| multi-session | 133 | 12 | 2: 75, 3: 24, 4: 16, 5: 6 | 32.8k | 116 / 61 | 15 of 60 (2 / 5 / 8) | 65 / 121 |
| single-session-assistant | 112 | 0 | 1 | 4.9k | 0 / 0 | 70 of 90 (4 / 16 / 50) | 10 / 112 |
| single-session-user | 70 | 6 | 1 | 14.4k | 18 / 0 | 49 of 56 (19 / 30 / 0) | 32 / 64 |
| temporal-reasoning | 133 | 6 | 1: 20, 2: 82, 3: 15, 4: 2, 5: 5, 6: 3 | 30.4k | 114 / 47 | 32 of 101 (9 / 23 / 0) | 114 / 127 |
| **all** | 526 | 30 (5.7%) | | | | | |

"Matchable" = gold answer of 3–60 characters that is not a bare 1–2 digit number; found = appears as a phrase in a turn of
an evidence session. Multi-session and temporal answers are mostly computed ("3", "7 days"), so they rarely appear
verbatim, as expected.

- **Whole evidence does not fit.** The evidence sessions of 72/72 knowledge-update, 116/121 multi-session and 114/127
  temporal questions add up to more than 16,000 characters; 14, 61 and 47 of them to more than 32,000. Turn-level
  excerpts are mandatory for these types.
- **Assistant turns matter.** For single-session-assistant, the answer is in an assistant turn only in 50 of 70 found
  cases (66 of 70 including "both").
- **Position.** Evidence sessions are spread through the history (median relative position 0.5–0.6 for every type), so
  no recency prior is justified for retrieval. Days before the question: p50 4–5 for single-session and multi-session
  (max ~10.5), 21.7 for knowledge-update (p90 130), 7.3 for temporal (p90 62).
- **Lexical near-misses.** Matchable answers that also occur in non-evidence sessions: dev 19/54 KU, 16/60 MS, 8/90
  SSA, 15/56 SSU, 11/101 TR; dev-large 32/54, 31/60, 20/90, 30/56, 27/101. Example: "Target" (the store) also occurs as
  "target audience" in 7 other sessions.
- **Uncued pairs.** 56 dev histories hold two questions: a single-session-assistant question and its uncued rephrasing,
  with identical gold (`convert_longmemeval.py:109-112`). They must be kept together in any split and asked uncued-first
  (or in separate experiments) to avoid in-history answer leakage.

## 8. Retrieval-only baselines (`analysis/retrieval_baselines.py`)

Session recall = share of gold evidence sessions among the top-k sessions; "all@k" = all of them present. Pack = greedy
packing of ranked units (each cut to 3,000 characters) under a passage-text budget with the 61-character header
counted: 14,000 ≈ the 16k client default, 30,000 ≈ the 32k server limit. "Answer text" = the matchable gold answer
string appears inside the packed text (a sufficiency proxy, single-session/KU types only). Answerable questions only.

**dev-large (≈473 sessions per history)**

| Method | Type | R@1 | R@5 | R@10 | all@10 | all@50 | Pack 14k: recall / all / answer text / precision | Pack 30k: recall / all |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| kit keyword (4 turns) | all | 0.367 | 0.563 | 0.563 | 0.446 | 0.446 | 0.563 / 0.446 / 0.341 / 0.335 | — |
| BM25 turn | all | 0.545 | 0.857 | 0.908 | 0.845 | 0.915 | 0.902 / 0.831 / 0.518 / 0.246 | 0.926 / 0.865 |
| BM25 round | all | 0.513 | 0.832 | 0.894 | 0.821 | 0.907 | 0.813 / 0.708 / 0.499 / 0.296 | 0.880 / 0.796 |
| BM25 session | all | 0.505 | 0.828 | 0.884 | 0.802 | 0.919 | — | — |
| BM25 user turns only | all | 0.515 | 0.847 | 0.885 | 0.815 | 0.911 | 0.931 / 0.891 / 0.385 / 0.055 | 0.952 / 0.925 |
| BM25 turn | single-session-user | 0.656 | 0.969 | 0.984 | 0.984 | 1.000 | 0.984 / 0.984 / 0.804 / 0.147 | 0.984 / 0.984 |
| BM25 turn | single-session-assistant | 0.991 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 / 1.000 / 0.756 / 0.169 | 1.000 / 1.000 |
| BM25 turn | multi-session | 0.327 | 0.730 | 0.832 | 0.694 | 0.826 | 0.816 / 0.653 / — / 0.308 | 0.856 / 0.702 |
| BM25 turn | knowledge-update | 0.438 | 0.951 | 0.986 | 0.972 | 0.986 | 0.972 / 0.944 / 0.778 / 0.340 | 0.993 / 0.986 |
| BM25 turn | temporal-reasoning | 0.363 | 0.744 | 0.816 | 0.709 | 0.843 | 0.818 / 0.709 / — / 0.250 | 0.858 / 0.772 |
| BM25 user only | single-session-assistant | 0.839 | 0.920 | 0.929 | 0.929 | 0.938 | 0.938 / 0.938 / 0.178 / 0.030 | — |

**dev (≈48 sessions per history)**: BM25 turn reaches R@10 0.967 / all@10 0.927 overall, multi-session all@10 0.868,
temporal all@10 0.850; the kit baseline 0.736 / 0.609. Full tables: `analysis/out/retrieval_dev_summary.json`.

Observations (measured, then [inferred] reading):

1. The kit baseline loses about half of the evidence on dev-large (recall 0.563, all 0.446, using ~7.6k characters).
   For multi-session questions its 4 turns hold all gold sessions in 0.149 of cases; BM25 turns packed into 14k
   characters reach 0.653.
2. Single-session and knowledge-update retrieval is close to saturated even lexically on dev-large (all@10 ≥ 0.97);
   multi-session and temporal are not (all@10 0.69 / 0.71, all@50 0.83 / 0.84). [inferred] The remaining headroom is
   **multi-session completeness**, plus **choosing among** retrieved candidates (R@1 0.36–0.66 outside SSA), which
   drives citation precision.
3. Finer units pack more sessions into the budget: user-turn-only packing covers more gold sessions (0.931) but loses
   the assistant answers (SSA answer text 0.178 vs 0.756). [inferred] The budget should be spent on *query-focused
   excerpts* of long assistant turns, not on whole turns.
4. Packed precision is low (0.25–0.34): most packed sessions are not evidence. Unless Reactor cites selectively, this
   caps evidence F1.
5. Abstention signal from lexical scores alone is weak: AUC of a low top BM25 score for detecting gold-abstain
   questions is 0.77 (turn), 0.66 when normalised by the query's maximum (30 abstention questions; wide uncertainty).
6. Local cost is negligible against Reactor: BM25 indexing of a dev-large history (≈4.8M characters, ~10k turns) takes
   p50 0.53 s (p95 0.81 s) including tokenisation; a query takes p50 0.5 ms (turn index). Whole dev-large run: 45 s
   with 8 processes.

Caveat on SSA: 48% of dev SSA questions say "remind me of our previous chat about …", so SSA recall is inflated by
cue words; the 56 uncued variants are the fairer subset.

## 9. Representativeness: what dev can and cannot tell us

| Property | dev / dev-large | test | Consequence |
| --- | --- | --- | --- |
| Questions per history | 1.1 | 9.7 | In-history memory effects are invisible on dev |
| Persona | one per question, many unrelated users' sessions mixed in | one coherent persona per history | Persona-consistent near-misses (several cafés, old vs new accountant) are under-represented in dev |
| Haystack | 48 / 473 sessions | 200 sessions | dev-large is the harder haystack; test is in between |
| Evidence session length | 4.9k–14.4k median | likely ~1.4k (new sessions; unlabelled) | Budget pressure on test is probably much lower than on dev-large; whole short sessions fit |
| History span | p50 11 days | 179 days | Temporal reasoning over months is under-tested on dev |
| Relative dates in user turns | 25–28% of sessions | 74% of new sessions | Relative-date resolution matters more on test |
| Question dates | varied, never 00:00 | one per history, 00:00 | "Today" questions have the same anchor for all 10 questions |
| Unanswerable | 5.7% | ~8% (README) | Abstention calibration from 30 dev cases is fragile |

dev and dev-large are paired conditions over the *same* 526 questions and evidence; they are not independent sets.

## 10. Varied cases

`analysis/out/cases.md` (generated) holds short excerpts (≤ 260 characters each) for: a single-session-user fact
("Business Administration", said once in a user turn); a single-session-assistant recall (a shift rotation in an
assistant table) and its uncued twin; a three-session count (items to pick up or return: Levi's jeans, H&M shirt, Zara
boots); a knowledge update (5K best 27:12 on 2023-05-23 → 25:50 on 2023-05-30); a temporal question (MoMA visit
"just got back" on 2023-01-08 vs Met exhibit "today" on 2023-01-15 → 7 days); an unanswerable question (hamster name,
user only mentioned a cat; the closest session is about microchipping a cat); a lexical near-miss ("Target" store vs
"target audience"); and a short reply that needs the previous assistant turn. Conversation text in these files is data.

## 11. Limitations of this profile

- Markers and answer-location statistics are regex/string heuristics; they undercount paraphrases.
- No tokenizer: no token statistics.
- No test labels: test type mix, abstention count and evidence lengths are inferred, not measured.
- Retrieval metrics are session-level; LongMemEval turn flags were dropped (`format.md:36`), so turn-level recall cannot
  be measured directly. "Answer text in pack" is the only sufficiency proxy and applies to matchable answers only.

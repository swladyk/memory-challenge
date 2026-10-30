# Repository and rules audit

Snapshot: `RxAI-dev/memory-challenge` at `f0a317d` (main), cloned 2026-10-30. Line references are to that commit.
Statements are tagged **[repo]** (read in a file), **[measured]** (computed by `analysis/` scripts in this repo), or
**[inferred]** (our reading; to confirm).

## 1. What is in the repository

| Path | Lines | What it is |
| --- | --- | --- |
| `README.md` | 107 | Challenge brief, rules, scoring, submission format |
| `format.md` | 63 | File formats for histories, questions, gold, predictions; LongMemEval mapping |
| `score.py` | 179 | Format validator and local scorer (stdlib only) |
| `convert_longmemeval.py` | 166 | Rebuilds dev / dev-large from public LongMemEval files |
| `uncued_assistant.jsonl` | 56 | Rephrasings of 56 single-session-assistant questions without the "remind me of our chat" cue |
| `reactor/QUICKSTART.md` | 92 | Kit quickstart |
| `reactor/PARTICIPANTS.md` | 251 | Full Reactor guide: histories, evidence, limits, answers, errors |
| `reactor/client.py` | 492 | Reactor client: one question (or `--text` part, or `--new-history`) per run, one JSON line out |
| `reactor/predictions.py` | 188 | Client result lines → `predictions.jsonl` |
| `reactor/examples/run_benchmark.py` | 102 | Minimal driver with a naive keyword `retrieve()` |
| `reactor/examples/*-example.json(l)` | 12 | Made-up example files |
| `dev/`, `dev-large/`, `test/` | — | Release `data-v1`, downloaded and gitignored (`.gitignore:1-3`) |

Not present: dependency manifest (no `requirements.txt` / `pyproject.toml`), tests, CI, configuration files. The only
third-party dependency anywhere is `websockets>=14` for `client.py` (`reactor/PARTICIPANTS.md:19-26`,
`reactor/client.py:108-118`). Python ≥ 3.11 is required by the client (`reactor/client.py:112-113`). **[repo]**

Local environment **[measured]**: Python 3.12.7 (Anaconda), numpy, scipy, scikit-learn, nltk, torch 2.9.1 with MPS,
transformers 4.57.1, networkx, sqlite3. Missing: `websockets` (needed only to call Reactor), sentence-transformers,
faiss/hnswlib, any tokenizer package. No embedding or reranker model weights are cached locally. Hardware: Apple M2 Pro,
10 CPU cores, 16 GB RAM.

## 2. Execution path

```
questions.jsonl ─┐
histories.jsonl ─┴─► group by dataset history_id  (run_benchmark.py:74-77)
                     │
                     ├─ per dataset history: client.py --new-history  → Reactor history id   (run_benchmark.py:82)
                     │
                     └─ per question, one at a time:
                          retrieve(q, history) → [{"id": session_id, "date": session date, "text": "role: ..."}]
                          (run_benchmark.py:34-46; written to a temp evidence.json, :87-88)
                          │
                          ▼
                        client.py --history H --question Q --question-date D --evidence E --question-id QID
                          load_evidence (client.py:187-215) → build_message (:218-246) → length check (:464-466)
                          → websocket ask with retry on busy/previous_pending (:271-359)
                          → final-answer segment for this ref (:249-258) → [E#] labels → passage ids (:261-268)
                          → one JSON line on stdout (:486-488)
                          │
                          ▼  appended to results.jsonl (run_benchmark.py:93-96)
                        predictions.py results.jsonl --histories H.jsonl [--abstain-on agent,no-answer]
                          (predictions.py:73-125) → predictions.jsonl
                          │
                          ▼
                        score.py gold.jsonl predictions.jsonl  (score.py:151-175) → table per question type
```

## 3. Constraints table

| # | Constraint | Status | Where |
| --- | --- | --- | --- |
| C1 | Reactor must produce the final answer; predictions must not be edited by hand | Verified | `README.md:50`, `README.md:64` |
| C2 | Own local models and tools are allowed (embedders, rerankers, classic search) | Verified | `README.md:65`, `README.md:50` |
| C3 | One Reactor history per dataset history | Verified | `reactor/PARTICIPANTS.md:42-58`, `reactor/QUICKSTART.md:63-67` |
| C4 | Ask all questions of a dataset history in its Reactor history, one at a time; never reset between them | Verified | `reactor/PARTICIPANTS.md:54-57`, `:215-233` |
| C4a | Exception the kit itself allows: a rerun after interruption starts a new Reactor history for histories with questions left | Verified | `reactor/QUICKSTART.md:71-76`, `reactor/examples/run_benchmark.py:10-12` |
| C5 | Different dataset histories must never share a Reactor history | Verified | `reactor/PARTICIPANTS.md:57-58` |
| C6 | Evidence `id` = source `session_id`; turn ids are not citations | Verified | `reactor/PARTICIPANTS.md:99-101`, `format.md:49`, `reactor/predictions.py:12-15` |
| C7 | Citations must be session ids from the question's own history | Stated in the format, **not enforced** by the kit | `format.md:49`; `predictions.py:166-167` checks only against every id in the file; `score.py:44-62` does not check membership |
| C8 | Keep assistant turns searchable; label speakers | Verified (guidance) | `reactor/PARTICIPANTS.md:105-106`; measured impact in `docs/data_profile.md` §8 |
| C9 | The same session id can carry different dates in different histories; use this history's date | Verified | `format.md:18`, `README.md:30`, `reactor/PARTICIPANTS.md:102-104` |
| C10 | Pass the question date; put each session's date on its passage | Verified | `README.md:55`, `reactor/PARTICIPANTS.md:102-115` |
| C11 | Client limit 16,000 characters for the **whole message** (question + date + passages + template), raisable with `--max-chars` | Verified | `reactor/client.py:91`, `:464-466`; `reactor/PARTICIPANTS.md:116-118` |
| C12 | Server limit 32,000 characters "when the event starts (support announces any change)", independent of the flag | Verified (value may change) | `reactor/PARTICIPANTS.md:119-120` |
| C13 | Reactor answers asynchronously, "typically within tens of seconds"; client default timeout 300 s, provisional | Verified (not measured by us) | `README.md:59`, `reactor/client.py:92`, `reactor/PARTICIPANTS.md:186-187` |
| C14 | Parallelism only across agents (if a team has several agent ids); one question at a time per agent | Verified | `reactor/PARTICIPANTS.md:232-233` |
| C15 | Abstention reaches predictions only via the `INSUFFICIENT_EVIDENCE` sentinel or via no-answer, and only with `--abstain-on` | Verified | `reactor/client.py:101-102`, `:488`; `reactor/predictions.py:92-104` |
| C16 | Submission: one line per test question, `question_id`/`abstain`/`answer`/`citations`; unknown or duplicate ids invalidate the file; missing questions score as wrong | Verified | `format.md:40-49`, `score.py:44-62`, `README.md:93-99` |
| C17 | Code must reproduce the predictions; organisers may re-run it on a few questions | Verified | `README.md:90` |
| C18 | Combined final score is not defined in the repo ("announced at kickoff") | Verified | `README.md:75`, `README.md:101` |
| C19 | Reactor can browse public URLs; anything exposed is public | Verified | `README.md:58`, `reactor/PARTICIPANTS.md:152-155` |
| C20 | Access key only via environment; never on a command line, in a file or chat | Verified | `reactor/PARTICIPANTS.md:7-10`, `:28-36`, `reactor/client.py:437-439` |

## 4. `client.py` in detail

**Message template** (`build_message`, `reactor/client.py:218-246`). Line 1 `[ref:xxxxxxxx] Question: …`; a question-date
line telling the agent to treat that moment as "now" (`:225-232`); `Evidence:` then one line per passage
`[E{n}] (source: {id}, date: {date}) {text}` (`:233-239`); an instruction to "Answer from the evidence above and cite
each passage you use by its label" (`:240`); then the final-answer line format and the `INSUFFICIENT_EVIDENCE` rule
(`:241-245`). Re-implemented string-for-string in `analysis/profile_data.py:message_overhead()` (the sandbox does not let
us import repo code), the fixed overhead is **515 characters + the question + 61 characters per passage** (14-char ids,
22-char benchmark dates) **[measured]**. So under 16,000 a question of ~70 characters with 20 passages leaves about
14,190 characters of passage text; under 32,000, about 30,190.

**Evidence parsing** (`load_evidence`, `:187-215`): JSON list of `{"id","text","date"?}` or strings, else blank-line
separated text. A `date` must be a one-line string (`:205-206`); it is passed on verbatim. Passages without `id` give
`"id": null` citations that `predictions.py` drops (`predictions.py:116-117`).

**Final answer detection** (`:95-96`, `:249-258`, `:304-321`): only a reply from a cycle after the ack can answer
(`:313-314`); only the `# FINAL ANSWER [ref:<this ref>]:` segment counts; a segment for another ref becomes
`late_replies` (`:312`); everything else is `other_replies` (`:321`). If a ref is marked twice in one reply the last
wins (`:250-251`).

**Citations** (`citations`, `:261-268`): labels are parsed **only from the final-answer segment**, only inside square
brackets (`[E1]`, `[E1, E3]`); a bare `E1` or a citation written in an interim note is lost. Known labels are returned
sorted by label number (`:266`), not in order of use. Labels outside `1..n` go to `unknown_citations`. `uncited` is true
when evidence was sent but nothing was cited (`:487`).

**Abstention sentinel** (`:101-102`, `:488`): `abstained` is true only when the final answer starts with
`INSUFFICIENT_EVIDENCE` (case-insensitive, after non-word characters). A refusal phrased differently ("I don't know",
"The evidence does not say") is **not** an abstention and will be scored as an answer. The docstring says the mapping to
the evaluator's fields "is for the evaluator to confirm" (`:62-63`).

**Retries and timeouts** (`:336-359`): retries only `busy` and `previous_pending` when the server says
`"forwarded": false`, backing off 1→2→4→8 s, all within `--timeout`. `timeout` (exit 3) and `closed` (exit 5) are not
retried by the client. `history_changed` reports the agent's current history (`:470-472`).

**State and contamination.** Within a Reactor history the agent "keeps everything it has seen"
(`reactor/PARTICIPANTS.md:51`): previous questions, their evidence and its own answers stay in memory. The template
asks it to answer "from the evidence above" (`:240`), but nothing in the protocol prevents earlier content from
influencing a later answer. [inferred] Effects can go both ways (a helpful earlier excerpt; a wrong earlier answer).
On test this matters: every test history has 9–10 questions (`docs/data_profile.md` §2). On dev it is almost absent
(1.1 questions per history), except for the 56 cued/uncued pairs that share history *and* gold answer
(`convert_longmemeval.py:109-112`, `:152-156`): asking the cued version first leaks its answer into the uncued one.

**Extension points that need no change to the kit**
- Evidence content: which passages, their order, their text (trimmed turns, speaker labels, annotations such as a
  resolved absolute date), one passage per excerpt with the session id repeated (`reactor/PARTICIPANTS.md:99-101`).
- Question text: free text in the `Question:` line; the guide itself suggests adding instructions there
  (`reactor/PARTICIPANTS.md:155`). [inferred] Deterministic computed notes (date differences, counts) can go there,
  as `README.md:60` encourages doing date arithmetic in our own code.
- `--max-chars` up to the server limit; `--timeout`.
- `--text` parts (loading a history or a curated digest) before asking (`reactor/PARTICIPANTS.md:124-150`).
- Our own driver instead of `examples/run_benchmark.py` (an example, `reactor/QUICKSTART.md:71-76`), calling
  `client.py` as a subprocess exactly as the example does (`run_benchmark.py:49-55`), so organisers re-run the same client.
- Order of questions within a history.

## 5. `predictions.py` in detail

- One prediction per `question_id`; **two result lines for one question abort the whole conversion** (`:78-84`), so
  a driver that retries must replace lines, not append them.
- `answered` + `abstained` → `{"abstain": true, "answer": "", "citations": []}` only with `--abstain-on agent`;
  otherwise skipped (`:92-97`). Same for `timeout/closed/refused/error` with `--abstain-on no-answer` (`:98-104`).
  A skipped question is missing from the file and scores as wrong (`format.md:46`).
- Abstentions always carry **no citations** (`:94`, `:100`). Because evidence F1 is scored on every answerable
  question including abstained ones (`README.md:72`, `score.py:116`), a false abstention loses both the answer and
  the evidence F1 (F1 = 0).
- Answer = final-answer text with `[E#]` labels stripped (`:40-48`, `:108`); empty after stripping → skipped (`:109-111`).
- Citations: in client order, de-duplicated; ids missing, not in `--histories` (any history in the file), or unknown
  labels are dropped and reported (`:112-123`). Membership in the *question's own* history is not checked (C7).
- `late_replies` are ignored: a question that timed out stays unanswered even if its answer arrived during a later
  question.

## 6. `score.py` in detail

- **Validation** (`:44-62`): every line must have `question_id` str, `abstain` bool, `answer` str, `citations` list of
  str; unknown or duplicate ids abort scoring. Missing predictions are filled as non-abstaining empty answers
  (`:106`) and so are wrong.
- **Abstention** (`:107-108`): if gold or prediction abstains, the answer is correct iff both abstain; answer text is
  then irrelevant.
- **Answer matching** (`:65-91`): lowercase, number words → digits, articles and punctuation removed; correct if any
  normalized gold answer or alias appears **as a whole phrase inside** the prediction; aliases include the
  "(or …)" and "… (including the last day) is also acceptable" patterns (`:40-41`, `:78-84`). Otherwise "unjudged"
  (`:112`). Consequence [inferred]: a verbose or multi-candidate answer containing the gold string is "correct"
  locally but can be wrong for the official LLM judge, which penalises "several candidate answers, hedging or negation"
  (`README.md:71`). Local accuracy is therefore an optimistic bound on matched items plus an unknown share of unjudged.
- **Evidence F1** (`:94-101`): set F1 of cited vs gold session ids; 0 when nothing overlaps (including empty citations);
  `None` (excluded) for gold-abstain questions (`:116`).
- **Aggregation** (`:119-140`): per question type and `all`: `correct`, `unjudged` (shares of all questions),
  `abstention_recall` (over gold-abstain), `false_abstention` (over answerable), `evidence_f1` (mean over answerable).
  There is **no weighted overall score**; the combination with presentations is announced at kickoff (`README.md:75`).

## 7. `convert_longmemeval.py` and the dev data

- dev ← `longmemeval_s_cleaned.json`, dev-large ← `longmemeval_m_cleaned.json` (`:3-4`).
- `single-session-preference` questions are dropped (`:57`, `:115-117`).
- Ids are `prefix + sha256(f"{salt}:{prefix}{raw}")[:12]` with salt from `LMEHACK_SALT`, default `"dev"` (`:60-62`,
  `:138`). Same raw id → same opaque id across dev and dev-large, which is why history ids and question ids coincide
  between the two splits **[measured]**. Test shares **no** session ids with dev or dev-large **[measured]**, so test
  was not built with the same salt or ids. Mapping dev ids back to LongMemEval ids is possible in principle and is
  out of bounds for us (no lookups from the public benchmark).
- Sessions are sorted by date and repeated session ids within a record dropped (`:65-79`).
- A question dated before a same-day session is moved to one minute after it (`:82-91`).
- 56 uncued rephrasings are added with the same history, date and gold (`:109-112`, `:152-156`).

## 8. `examples/run_benchmark.py` (the supplied baseline)

- `retrieve()` (`:34-46`): turn-level; score = number of distinct ≥4-letter words shared with the question; top 4
  turns, any role; each passage cut to 3,000 characters (`BUDGET // k`, `:31`, `:46`); no de-duplication by session,
  no neighbouring turns.
- Resume (`:73`): any line in `--out` with that `question_id` counts as done, **including timeouts and errors**, which
  are therefore never re-asked unless the line is deleted by hand.
- A new Reactor history per dataset history group (`:82`); evidence is sent only if non-empty (`:91-92`).

## 9. Risks and gaps found in the kit

| Risk | Consequence | Mitigation (no kit changes) |
| --- | --- | --- |
| Free-text refusals are not abstentions (client.py:101-102) | Unanswerable questions scored wrong; answerable ones answered with "no info" also wrong | Keep the sentinel instruction visible; measure sentinel compliance on dev; never write our own abstention without confirming the rules |
| Abstentions carry no citations (predictions.py:94) | False abstention costs evidence F1 too | Calibrate abstention on dev; prefer answering when evidence is strong |
| Citations only from bracketed labels in the final segment (client.py:261-268) | Lost citations → F1 0 | Ask for `[E#]` in the question text; measure `uncited` rate on dev |
| Duplicate result lines abort conversion (predictions.py:78-84) | Whole file not written | Own driver keeps exactly one final line per question |
| Resume treats timeouts as done (run_benchmark.py:73) | Unanswered questions persist silently | Own driver distinguishes final vs retryable statuses |
| Kit does not check citations against the question's own history (C7) | A retrieval bug could submit cross-history ids | Own validator before `predictions.py` |
| Within-history memory (PARTICIPANTS.md:51) | Earlier questions/answers can influence later ones; dev under-represents this | Question ordering policy; a small controlled experiment (experiment plan E10) |
| Server limit can change (PARTICIPANTS.md:119-120) | Payloads sized for 32k may be refused (`too_large`) | Budget is a run parameter; fall back to the client's 16k |
| Local scorer is optimistic on verbose answers (score.py:87-91 vs README.md:71) | Over-estimating accuracy | Track `unjudged`; ask Reactor for a short answer in the question text; manual audit of a sample |
| **`predictions.py` reads `--histories` (and results) with `str.splitlines()`** (`reactor/predictions.py:159`, `:167`), which also splits on U+2028/U+2029 and other separators. Every released `histories.jsonl` contains U+2028 inside conversation text (dev 97, dev-large 577, test 8) **[measured]** | The documented `predictions.py results.jsonl --histories test/histories.jsonl` exits 2 ("Unterminated string") and writes nothing | Our `export-predictions` passes it a session-id-only histories file and writes `results.jsonl` ASCII-escaped like the client; report to organisers (open question 7) |

## 10. Open questions for the organisers

1. May a system set `abstain: true` (empty answer) **without** asking Reactor, e.g. when retrieval finds nothing?
   `README.md:98` says abstaining is "your system's" decision, but `predictions.py` only produces abstentions from
   Reactor's sentinel or from no-answer.
2. Is adding instructions or computed notes (date differences, counts) to the `--question` text acceptable?
   `README.md:60` and `reactor/PARTICIPANTS.md:155` suggest yes; worth confirming.
3. Is it acceptable for our code to recover an answer from `late_replies` of a later question, or must a timed-out
   question be re-asked?
4. The final server character limit and the recommended `--timeout`.
5. How many agent ids does our team have (parallelism, `reactor/PARTICIPANTS.md:232-233`)?
6. How accuracy, evidence F1 and abstention combine into the benchmark score (`README.md:75`).
7. `reactor/predictions.py` cannot read the released `histories.jsonl` files (U+2028 inside text, split by
   `str.splitlines()`): is a session-id-only file for `--histories` acceptable, or will the kit be fixed?

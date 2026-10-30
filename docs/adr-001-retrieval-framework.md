# ADR-001: Retrieval framework for the Memory Retrieval Challenge

Status: accepted for implementation · Date: 2026-10-30 · Branch: `feat/retrieval-framework`

## Context (verified against the checked-out kit, commit `f0a317d`; details in `docs/repository_audit.md`)

| Contract | Source |
| --- | --- |
| Reactor produces the final answer; predictions are never edited by hand | `README.md:50`, `README.md:64` |
| Local models, indexes and preprocessing are allowed | `README.md:65` |
| One Reactor history per dataset history; its questions one at a time, no reset between them; never mix users | `reactor/PARTICIPANTS.md:42-58` |
| A rerun after interruption starts a new Reactor history for histories with questions left | `reactor/QUICKSTART.md:71-76` |
| Evidence `id` = session id; `date` = that session's date *in this history*; label speakers; keep assistant turns | `reactor/PARTICIPANTS.md:96-106`, `format.md:18` |
| Whole message ≤ `--max-chars` (default 16,000) at the client; ≤ 32,000 at the server (may change) | `reactor/client.py:91`, `:464-466`; `reactor/PARTICIPANTS.md:116-120` |
| Answer = the `# FINAL ANSWER [ref:<8 hex>]:` segment of a reply from a later cycle; `[E#]` labels in it map to passage ids | `reactor/client.py:95-96`, `:249-268`, `:304-321` |
| Abstention = final answer starting `INSUFFICIENT_EVIDENCE` | `reactor/client.py:101-102`, `:488` |
| Client retries only `busy`/`previous_pending` when `"forwarded": false`; timeouts and closed chats are not retried | `reactor/client.py:336-359` |
| `predictions.py`: one line per question or it aborts; abstentions only with `--abstain-on`; abstentions carry no citations; citations checked against all ids in the file, not per history | `reactor/predictions.py:78-125`, `:166-167` |
| `score.py`: substring match after normalisation; unmatched answers are "unjudged" (official scoring uses an LLM judge); no weighted overall score | `score.py:65-140`, `README.md:71-75` |

Measured facts that shape the design (`docs/data_profile.md`, `docs/eda_deep_dive.md`): evidence for most
multi-session/temporal/update questions exceeds one message; assistant turns hold 87% of text and SSA answers; BM25
over turns is near-saturated for single-session types but misses ~20% of MS/TR evidence sessions; test rankings are
2–3× more crowded; test sessions use forward-looking relative dates; answers in assistant turns are mostly list items.

## Decisions

1. **Dependency-free core.** The baseline (loader, windows, BM25, graph, PPR, packing, harness, evaluation) uses only
   the Python 3.11+ standard library. Dense retrieval and reranking are optional extras (`pip install -e .[dense]`) that
   fail with a clear error when missing. No vector or graph database: histories are ≤ ~10k windows, so exact search
   and in-memory adjacency are sufficient (to be re-checked if dense is enabled).
2. **Content vs occurrence.** `SessionContent` (id, hash, turns) is immutable and shareable; `SessionOccurrence`
   (history, session, date, position) is per history. Windows are derived from content only; dates, relative-date
   resolution and graph nodes are per occurrence. Every emitted passage is checked for membership in the question's
   history.
3. **Windows, not sessions.** Turn-level windows with exact source spans; long turns split on blocks (paragraphs, list
   items, table rows) without splitting a list item; list items keep their ordinal, list id and lead-in line; short
   user replies carry the tail of the preceding assistant turn as context. Original text is always recoverable.
4. **Retrieval pipeline.** Query plan (rule-based hints, never gold types) → BM25 (+ optional dense) → RRF fusion →
   optional rerank → bounded structural graph expansion (optional PPR as an extra fusion source) → heuristic coverage
   check → at most one recovery round → packing. No query-time LLM calls; no loops beyond the one recovery round.
5. **Packing against the real message.** One passage per session (windows merged in source order, overlaps removed),
   chronological order, derived notes clearly marked as not quoted. Message length is computed with the official
   `build_message` when it can be imported, else with a mirrored template that a test checks against the official one.
   Payloads over the limit are never emitted.
6. **Temporal handling is conservative.** Dataset dates are parsed per occurrence; relative expressions are resolved
   against the mentioning session's date only when unambiguous (today, yesterday, tomorrow, last &lt;weekday&gt;, N days
   ago, this/next/last week…); everything else is left as written. Derived arithmetic lists inputs, units, sources,
   operation and result, and is metadata for Reactor, never a submitted answer.
7. **Reactor harness wraps the official client** as a subprocess (the organisers re-run that client), one thread per
   agent id, sequential within an agent, append-only attempt log, explicit accepted-result selection, and a strict split
   between Reactor abstention, retrieval weakness, timeouts and protocol failures. Live calls require an explicit flag
   and the key from `REACTOR_ACCESS_KEY`; the key is never read, logged or put on a command line by our code.
8. **Evaluation hygiene.** Gold is read only in `evaluation/`. Paired dev/dev-large questions and cued/uncued twins are
   kept together in any partition. Retrieval session hits are reported as hits, not as proof the answer span was sent.

## Consequences

- The baseline runs anywhere with Python 3.11+; optional components are measurable add-ons, not prerequisites.
- Graph features are judged by ablation against the lexical baseline, not assumed to help.
- Real Reactor runs, model downloads and organiser questions (`docs/repository_audit.md` §10) remain open approvals.

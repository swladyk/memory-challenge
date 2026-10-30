# EDA deep dive: abstention, misses, near-misses, dates

Follow-up to `docs/data_profile.md`. All numbers are **measured** by `analysis/deep_dive.py` (75 s for all three
splits, 8 processes; output `analysis/out/deep_dive.json`). Ranking uses BM25 over turns as in
`analysis/retrieval_baselines.py`, with question boilerplate ("remind", "previous chat", "mentioned"…) dropped from
the query. Gold is joined only after ranking, for diagnosis; test is analysed from inputs only.

```bash
python -I analysis/deep_dive.py --root . --workers 8
```

## Summary

| # | Finding | Design consequence |
| --- | --- | --- |
| 1 | Unanswerable questions are mostly **near-misses**, not absent topics: the entity exists, the asked attribute doesn't | Abstention must be decided at the attribute level, by Reactor over good evidence; no lexical shortcut |
| 2 | About **one in five** multi-session and temporal evidence sessions ranks below the top 10, and those are **incidental asides** in off-topic sessions with little word overlap | Query expansion, dense retrieval or fact-augmented keys target these; plain BM25 tuning won't |
| 3 | Test rankings are **two to three times more crowded** at the top than dev-large's | Discrimination (reranking, attribute matching, dates) matters more on test than dev suggests |
| 4 | Test's new sessions use **future-tense dates** ("tomorrow", "next week", "this weekend") far more than dev | The date resolver must handle forward references; dev barely tests them |
| 5 | Answers in assistant turns are **mostly list items**, a quarter of the way into a ~1.2k-character turn | Excerpt at the line/list-item level and keep the list's lead-in line |
| 6 | Test questions' key terms **recur across sessions months apart** (42% of questions; median spread 93 days) | Updates and same-entity near-misses across time are the norm on test; order evidence by date |
| 7 | Duplicate sessions under different ids are rare for evidence (2 of 916) | Citation risk from duplicates is negligible; no special handling needed |

## 1. Abstention: what makes a question unanswerable

Signals computed per question without gold, then compared between gold-unanswerable (30) and answerable (496)
questions. AUC = probability an unanswerable question scores "more absent" (or, for BM25, lower) than an answerable one.

| Signal | dev AUC | dev-large AUC |
| --- | --- | --- |
| Top BM25 score (lower = more likely unanswerable) | 0.783 | 0.773 |
| Share of question terms found nowhere in the history | 0.692 | 0.560 |
| Same, weighted by term rarity | 0.692 | 0.560 |
| Share of question terms found in no user turn | 0.687 | 0.569 |

- A question term missing from the whole history flags 43% of unanswerable dev questions, but also 5.4% of answerable
  ones. Answerable questions outnumber unanswerable ones about 16 to 1, so roughly two of every three flagged questions
  are answerable after all. On dev-large the flag rate falls to 13% vs 1.4%: with ten times more sessions, the term
  usually appears somewhere, often in another user's filler session.
- Unanswerable examples show why (dev): "How long was I in Korea for?" and "How many fish are there in my 30-gallon
  tank?" have every term present in the history; "What is the name of my hamster?" lacks only "hamster" while the cat
  is discussed. The entity or topic exists; the specific fact does not.

**Consequence.** No lexical or score threshold separates the two cleanly (best AUC 0.78). The retrieved evidence for an
unanswerable question will look relevant. Abstention has to come from checking whether the evidence states *the asked
attribute for this user*, which is Reactor's sentinel decision; our job is to supply the closest evidence with dates
and speakers so that the check is possible. A low score can still feed a bounded second retrieval round (experiment
E7) but should never abstain on its own.

## 2. Why retrieval misses multi-session and temporal evidence (dev-large)

Rank of each gold evidence session among ~473 sessions (BM25 over turns, session = best turn):

| Type | Evidence sessions | Ranked below 10 | Median rank | p90 rank | Question-term coverage, hit vs miss (median) |
| --- | --- | --- | --- | --- | --- |
| single-session-user | 64 | 2 | 1 | 4 | 0.83 vs 0.20 |
| single-session-assistant | 112 | 0 | 1 | 1 | 0.88 vs — |
| multi-session | 316 | 63 (20%) | 2 | 29 | 0.67 vs 0.50 |
| knowledge-update | 144 | 2 | 2 | 3 | 1.00 vs 0.19 |
| temporal-reasoning | 280 | 58 (21%) | 2 | 46 | 0.65 vs 0.33 |

Coverage = share of the question's content terms that appear anywhere in the session. Nine missed sessions share no
question term at all. Misses are no longer than hits (median 15–16k vs 14–15k characters), and their best-matching turn
is a user turn about half the time.

What the misses look like (dev-large, the user turn sharing most question terms, abbreviated):

- *How old was I when Alex was born?* (rank 294, no shared term): a session about a career change, in which no turn
  shares a word with the question.
- *How many years older is my grandma than me?* (rank 239, no shared term): "…planning a trip to Europe… by the way,
  I'm still getting used to being in my 30s…"
- *How many years older am I than when I graduated from college?* (rank 63): "As a 32-year-old Digital Marketing
  Specialist at TechSavvy Inc., I believe the certification…"

**Consequence.** The missing evidence is a personal fact dropped in passing ("by the way…") inside a session about
something else, phrased differently from the question (age ↔ "32-year-old", "in my 30s"). This is vocabulary mismatch
plus topic mismatch. Candidates, in order of cost: query expansion for attribute words (age → years old, born,
birthday; how long → since, started, years); dense retrieval (E3); fact-augmented keys, which the LongMemEval paper
reports lifting session R@10 on its _M split (self-reported, Stella retriever: 0.783 → 0.862), at the price of one
local LLM call per session. A second retrieval round seeded by the first round's entities (E7) helps only when the
first round finds part of the evidence.

## 3. Near-miss crowding: test vs dev

For each question: how many other sessions score within 90% / 75% of the top session, and the relative gap between
the first and second session.

| | Sessions within 90% of the top (median / p90) | Within 75% (median / p90) | Gap between 1st and 2nd (median) |
| --- | --- | --- | --- |
| dev | 0 / 1 | 0 / 2 | 29% |
| dev-large | 0 / 1 | 1 / 4 | 20.5% |
| test | 1 / 4 | 2 / 8.5 | 7.8% |

Test was built with near-misses (`README.md:32`), and it shows: the top of a test ranking is two to three times more
crowded than dev-large's, with a median gap of under 8%. In test, 76% of each question's top-10 sessions are new
persona sessions (median 80%), against 22% of all sessions. Lexical retrieval already concentrates on the persona's
sessions without any artifact feature; the hard part is choosing among them.

For reference, precision@10 against gold on dev-large is 0.16 (median 0.2): most of any top 10 is not evidence.

**Consequence.** Reranking and attribute-level matching (who, what, which value, when) carry more weight on test than
dev metrics suggest, and evidence F1 will depend on Reactor citing selectively among similar sessions. Dates on every
passage are needed to break ties between old and new values.

## 4. Relative-date phrasing

Share of sessions whose user turns contain each phrasing (one session can match several):

| Phrasing | test, new sessions (666) | dev-large, temporal evidence (280) | dev-large, sample of all (28,419) |
| --- | --- | --- | --- |
| Any of these | 82% | 92% | 41% |
| today / tonight / this morning | 36% | 43% | 7% |
| this week / weekend / month / year | 26% | 10% | 5% |
| tomorrow | 14% | 0.4% | 0.5% |
| on &lt;weekday&gt; | 13% | 4% | 3% |
| next week / weekend / month / year | 12% | 6% | 3% |
| last week / weekend / month / year | 10% | 13% | 8% |
| recently / the other day / just … | 9% | 47% | 19% |
| &lt;Month&gt; &lt;day&gt; | 8% | 15% | 6% |
| yesterday | 7% | 4% | 1% |
| in / for N days or weeks (ahead) | 5% | 4% | 3% |
| in / since / by &lt;Month&gt; | 5% | 10% | 3% |
| this / next &lt;weekday&gt; | 3% | 1% | 0.4% |
| N days / weeks / months ago | 1% | 16% | 6% |
| last &lt;weekday&gt; | 1% | 2% | 2% |

**Consequence.** Dev's temporal evidence talks about the past ("recently", "N days ago"); test's persona sessions talk
about now and the near future ("today", "this weekend", "tomorrow", "next week", "on Friday"). A resolver built and
checked on dev would barely exercise forward references. Priorities for the resolver: today/tonight (= session date),
this week/weekend, tomorrow, on/next/this &lt;weekday&gt; (with a rule for "on Friday" said on a Friday), next/last
week/month, explicit month-day with year inference. Planned events also raise a question type dev under-represents:
did the planned thing happen, and when.

## 5. How answers in assistant turns are formatted (dev, single-session-assistant)

Of 66 questions whose gold answer string was located on a line of an assistant turn: 44 in a numbered list item, 4 in
a bullet item, 18 in a plain paragraph; none in a table row by this test (table answers exist but rarely match the
gold string verbatim). The answer line sits a median 24% of the way into the turn (p75 59%); those turns are a median
1,171 characters.

**Consequence.** Line-level excerpting of assistant turns should keep whole list items plus the list's lead-in line
(which names what the list is), and the opening of the turn; cutting to sentences would split list items.

## 6. Recurring entities across a history

For each question, the two rarest question terms present in the history; how many sessions mention both in user turns,
and how far apart those sessions are.

| | Questions whose two rarest terms co-occur in 2+ sessions | Sessions with both (mean / p90) | Days between first and last such session (median, when 2+) |
| --- | --- | --- | --- |
| dev-large, all | 28% | 1.2 / 3 | 5.4 |
| dev-large, knowledge-update | 65% | 1.8 / 3 | — |
| test | 42% | 2.5 / 6 | 93 |

**Consequence.** On test, the entities a question asks about come back across sessions that are months apart, much more
than in dev-large. That is where updates ("my accountant *now*") and same-entity near-misses live. Payloads should
bring every dated mention of the entity, in chronological order, rather than the single best one; the optional claim
layer (architecture proposal §3) is aimed at exactly this.

## 7. Duplicate session contents

Sessions with identical content under different ids within one history: none in dev or test; 120 of 470 dev-large
histories have at least one such pair. Only 2 of 916 gold evidence sessions in dev-large have a duplicate in their
history. Citation loss from duplicates is negligible; no special handling is needed.

## What this changes in the plan

- **Experiment priority**: reranking (E5) and multi-session recall work (E3 dense, query expansion, E7 second round)
  move up; abstention calibration from scores (part of E7) moves down.
- **New experiment E2b — attribute query expansion**: a small hand-written map from question attributes to evidence
  phrasings (age, duration, count, price, date) applied to BM25; success = MS/TR all@10 +0.05 on dev-large tune split.
- **Temporal resolver tests** (implementation plan §4) gain forward-reference cases: "tomorrow", "this weekend",
  "next Friday", "on Friday" said on a Friday.
- **Excerpting** keeps list items and lead-in lines.
- **Payloads** carry all dated mentions of a recurring entity, chronologically.

## Limitations

- BM25 with stemmed bag-of-words; coverage and recurrence are lexical, so paraphrased mentions are undercounted.
- Abstention has only 30 positive cases; AUCs carry wide uncertainty (roughly ±0.1).
- Relative-date patterns are regexes; they over-count idioms ("these days") and miss paraphrases.
- Test findings describe inputs only; no test labels exist.

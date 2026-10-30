# hAlo — grounded memory answers with Reactor

A three-minute presentation outline. Final test run and final-harness results are pending.

## 1. The problem (30 seconds)
Long histories contain near misses, changing facts and dates. Our system chooses
short, relevant evidence; Reactor provides the final answer.

## 2. The pipeline (45 seconds)
Original question and date → final retrieval harness → dated session excerpts →
Reactor → citation mapping → validated predictions.
The replaceable HTTP harness owns indexing and retrieval. The included BM25 baseline
searches user and assistant turns with a character budget and excludes future sessions.
Reactor memory is reset between users. Every request retains its exact outgoing message.

## 3. Evaluation we can inspect (45 seconds)
Report local answer matches and unjudged answers separately, per type and overall.
Measure session evidence F1 on all answerable questions, including abstentions.
Measure abstention recall and false abstention separately. The official LLM judge
can reject alternatives, hedging and negation that local matching accepts.
Insert measured dev results here after the final harness run; no benchmark claim yet.

## 4. Reproducibility (40 seconds)
One prediction per question, with Reactor-produced answer and session citations.
Export rejects missing questions, failed calls, altered answers and wrong-history citations.
Checkpoints resume completed histories and rerun interrupted histories cleanly.
Package source, harness version, hashes, run records and predictions together.

## 5. Next experiment (20 seconds)
Compare the final harness against the lexical baseline on dev: retrieval precision,
latest-fact selection, date arithmetic and abstention decisions. Freeze the harness,
run test once, validate coverage and submit under the announced event rules.

Source: https://github.com/swladyk/memory-challenge/tree/front

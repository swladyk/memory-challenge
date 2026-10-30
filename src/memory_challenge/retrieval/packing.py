"""Evidence packing: choose windows for coverage and precision, render one passage per session, fit the message.

Selection is greedy over fused scores, with a bonus for the first window of a session not yet chosen (so one session
can't crowd out another), a floor relative to the top score (so unrelated sessions aren't added for diversity), and
caps per session and overall. Every addition is checked against the exact length of the message the official client
would send; a payload over the limit is never emitted. Each passage is the original text of its windows, merged in
source order, followed by an optional bracketed note that says it is computed. A manifest maps each ``E#`` label to
its session, windows, spans, scores and provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Config
from ..data.windows import parse_blocks, render
from ..reactor.message import message_length
from ..schemas import Candidate, Span
from ..text import tokenize
from .temporal import DatedItem, derived_dates, passage_notes

GUIDANCE = ("Notes for answering: the evidence passages are excerpts of the user's past conversations, each dated "
            "when that conversation happened. If a value changed over time, the most recent statement is current. "
            "Lines in [note: ...] brackets are computed by the retrieval system, not quotes. Cite every passage you "
            "use as [E#] and give only the answer.")


@dataclass
class PackResult:
    evidence: list[dict]
    question_text: str
    message_chars: int
    limit: int
    builder: str
    manifest: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    question_notes: list[str] = field(default_factory=list)


def _focus(content, span: Span, tokens: set[str], max_chars: int) -> Span:
    """A sub-span of at most ``max_chars`` around the lines sharing most words with the question."""
    text = content.turns[span.turn_index].content
    if span.end - span.start <= max_chars:
        return span
    lines, pos = [], span.start
    for part in text[span.start:span.end].split("\n"):
        if part.strip():
            lines.append((pos, pos + len(part)))
        pos += len(part) + 1
    if not lines:
        return Span(span.turn_index, span.start, span.start + max_chars, span.context)
    best = max(range(len(lines)), key=lambda i: len(tokens & set(tokenize(text[lines[i][0]:lines[i][1]]))))
    lo = hi = best
    while True:
        grew = False
        if hi + 1 < len(lines) and lines[hi + 1][1] - lines[lo][0] <= max_chars:
            hi, grew = hi + 1, True
        if lo > 0 and lines[hi][1] - lines[lo - 1][0] <= max_chars:
            lo, grew = lo - 1, True
        if not grew:
            break
    s, e = lines[lo][0], min(lines[hi][1], lines[lo][0] + max_chars)
    return Span(span.turn_index, s, e, span.context)


def _bullet_note(content, windows) -> str | None:
    """For position questions over bullet lists (which show no numbers), the items' positions as written."""
    parts = []
    for w in windows:
        for item in w.list_items:
            if item.numbered:
                continue
            turn_index = int(item.list_id.split(".")[0])
            text = content.turns[turn_index].content
            for b in parse_blocks(text):
                if b.kind == "item" and f"{turn_index}.{b.list_no}" == item.list_id and b.ordinal == item.ordinal:
                    first = " ".join(text[b.start:b.end].split())[:60]
                    parts.append(f"bullet #{item.ordinal}: '{first}'")
    return "list positions: " + "; ".join(dict.fromkeys(parts)) if parts else None


def _render(selected: dict[str, list[Candidate]], plan, index, cfg: Config, caps: dict[str, int] | None = None):
    history = index.history
    pcfg, tcfg = cfg.packing, cfg.temporal
    caps = caps or {}
    qtok = set(plan.tokens) | set(plan.expansion)
    sids = list(selected)
    if pcfg.chronological:
        sids.sort(key=lambda s: history.occurrence(s).position)
    evidence, manifest, dated = [], [], []
    for n, sid in enumerate(sids, 1):
        content = index.content(sid)
        occ = history.occurrence(sid)
        wins = [index.window(c.window_id) for c in selected[sid]]
        spans, trimmed = [], []
        for w in wins:
            cap = min(pcfg.max_window_chars, caps.get(w.window_id, pcfg.max_window_chars))
            for sp in w.spans:
                f = sp if sp.context else _focus(content, sp, qtok, cap)
                if f != sp:
                    trimmed.append(w.window_id)
                spans.append(f)
        text = render(content, spans)
        notes, res = passage_notes(text, occ.date, plan.question_dt, tcfg) if pcfg.notes else ([], [])
        if plan.ordinal is not None and pcfg.notes:
            bn = _bullet_note(content, wins)
            if bn:
                notes.append(bn)
        if notes:
            text += "\n[note: " + " | ".join(notes) + "]"
        evidence.append({"id": sid, "date": occ.date_str, "text": text})
        label = f"E{n}"
        manifest.append({"label": label, "session_id": sid, "date": occ.date_str, "position": occ.position,
                         "windows": [c.window_id for c in selected[sid]],
                         "spans": [[s.turn_index, s.start, s.end, s.context] for s in spans],
                         "scores": [round(c.score, 6) for c in selected[sid]],
                         "via": [c.via for c in selected[sid]], "trimmed": trimmed, "notes": notes})
        if occ.date:
            dated.append(DatedItem(label, sid, occ.date.date(), "session date", "mention", True))
        dated += [DatedItem(label, sid, r.start, f"'{r.surface}'", r.kind, r.exact) for r in res
                  if r.start == r.end and r.kind != "mention"]
    qnotes = derived_dates(dated, tcfg) if ("temporal" in plan.hints and tcfg.derived_calculations) else []
    qtext = plan.question
    if pcfg.guidance:
        qtext += "\n\n" + GUIDANCE
    if qnotes:
        qtext += "\n\n" + "\n".join(qnotes)
    return evidence, qtext, manifest, qnotes


def pack(plan, candidates: list[Candidate], index, cfg: Config) -> PackResult:
    pcfg = cfg.packing
    limit = pcfg.max_message_chars
    excluded: list[dict] = []
    caps: dict[str, int] = {}
    valid = []
    for c in candidates:
        if not index.history.has_session(c.session_id) or c.window_id not in index.by_id:
            excluded.append({"window": c.window_id, "reason": "not in this question's history: rejected"})
            continue
        valid.append(c)
    selected: dict[str, list[Candidate]] = {}
    evidence, qtext, manifest, qnotes = _render(selected, plan, index, cfg)
    n, builder = message_length(qtext, evidence, plan.question_date)
    if n > limit:
        raise ValueError(f"{plan.question_id}: the question alone makes a {n}-character message, over {limit}")
    if valid:
        floor = pcfg.score_floor
        pool = [c for c in valid if c.strength >= floor]
        excluded += [{"window": c.window_id, "reason": f"strength {c.strength:.2f} below floor {floor}"}
                     for c in valid if c.strength < floor][:50]
        rank = {c.window_id: i for i, c in enumerate(valid)}
        failures = 0
        while pool and failures < 8:
            best = max(pool, key=lambda c: (c.strength + (pcfg.novelty_bonus if c.session_id not in selected else 0),
                                            -rank[c.window_id]))
            pool.remove(best)
            sid = best.session_id
            if sid not in selected and len(selected) >= pcfg.max_sessions:
                excluded.append({"window": best.window_id, "reason": f"session cap {pcfg.max_sessions} reached"})
                continue
            if len(selected.get(sid, [])) >= pcfg.max_windows_per_session:
                excluded.append({"window": best.window_id, "reason": f"per-session cap {pcfg.max_windows_per_session}"})
                continue
            trial = {k: list(v) for k, v in selected.items()}
            trial.setdefault(sid, []).append(best)
            t_ev, t_q, t_man, t_notes = _render(trial, plan, index, cfg, caps)
            t_n, builder = message_length(t_q, t_ev, plan.question_date)
            room = limit - n - 250  # what is left for this window's text after its header, notes and separators
            if t_n > limit and room >= pcfg.min_trim_chars:
                # Too long as a whole: try once more cut to the room left, around its best-matching lines.
                trial_caps = {**caps, best.window_id: room}
                t_ev, t_q, t_man, t_notes = _render(trial, plan, index, cfg, trial_caps)
                t_n, builder = message_length(t_q, t_ev, plan.question_date)
                if t_n <= limit:
                    caps = trial_caps
            if t_n > limit:
                excluded.append({"window": best.window_id, "reason": f"would make the message {t_n} > {limit} chars"})
                failures += 1
                continue
            failures = 0
            selected = trial
            evidence, qtext, manifest, qnotes, n = t_ev, t_q, t_man, t_notes, t_n
        excluded += [{"window": c.window_id, "reason": "not reached (budget stop)"} for c in pool[:20]]
    if n > limit:  # unreachable by construction; checked explicitly so it holds even under python -O
        raise RuntimeError(f"packing produced {n} > {limit} characters")
    return PackResult(evidence, qtext, n, limit, builder, manifest, excluded[:200], qnotes)

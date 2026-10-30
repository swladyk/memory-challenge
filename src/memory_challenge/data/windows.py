"""Context-preserving windows over one session's original text.

Each turn is parsed into blocks (paragraphs, list items with their continuation lines, table rows). Blocks are packed
into windows of at most ``max_chars`` without splitting a block (an oversized block is cut at sentence boundaries).
A window that holds list items but not the list's lead-in line ("Here are seven tips:") carries that line as context;
a table row carries the table's header. A short user reply carries the tail of the assistant turn it answers.
Every piece is a ``Span`` of original text, so excerpts are recoverable exactly. Windows depend only on session
content and the window config, never on a history, so they can be shared across histories.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import WindowConfig
from ..schemas import ListItem, SessionContent, Span, Window

ITEM = re.compile(r"^([ \t]*)(?:(\d{1,3})[.)]|[-*•+])[ \t]+\S")
TABLE = re.compile(r"^[ \t]*\|")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class Block:
    start: int
    end: int
    kind: str  # "para" | "item" | "table"
    list_no: int | None = None
    ordinal: int | None = None
    numbered: bool = False
    lead_in: int | None = None  # index of the block introducing this item's list / this row's table


def _lines(text: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, pos + len(line)))
        pos += len(line) + 1
    return out


TABLE_SEPARATOR = re.compile(r"^[ \t]*\|[\s:|-]+\|?[ \t]*$")


def parse_blocks(text: str) -> list[Block]:
    """Paragraphs, list items (with continuation lines and sub-bullets) and table rows, with list numbering.

    A list runs across blank lines until a non-indented paragraph; its lead-in is the paragraph just before its first
    item. Numbered items keep their written number as ordinal; bullets get their 1-based position."""
    blocks: list[Block] = []
    cur: Block | None = None  # the block a following non-blank line may extend
    in_list, list_no, ordinal, list_lead = False, 0, 0, None
    table_head: int | None = None
    for s, e in _lines(text):
        line = text[s:e]
        if not line.strip():
            cur = None
            continue
        m = ITEM.match(line)
        indent = len((m.group(1) if m else line[: len(line) - len(line.lstrip())]).expandtabs(4))
        if TABLE.match(line):
            in_list, cur = False, None
            if table_head is None:
                table_head = len(blocks)
                blocks.append(Block(s, e, "para"))  # header row (+ separator): the table's lead-in
            elif TABLE_SEPARATOR.match(line) and len(blocks) - 1 == table_head:
                blocks[-1].end = e
            else:
                blocks.append(Block(s, e, "table", lead_in=table_head))
            continue
        table_head = None
        if m and indent <= 1:
            if not in_list:
                list_no, ordinal, in_list = list_no + 1, 0, True
                list_lead = len(blocks) - 1 if blocks and blocks[-1].kind == "para" else None
            ordinal += 1
            number = int(m.group(2)) if m.group(2) else None
            cur = Block(s, e, "item", list_no, number if number is not None else ordinal, number is not None, list_lead)
            blocks.append(cur)
            continue
        last_item = blocks[-1] if blocks and blocks[-1].kind == "item" else None
        if last_item is not None and (cur is last_item or indent > 1):
            last_item.end = e  # continuation line, indented paragraph or sub-bullet of the current item
            cur = last_item
            continue
        if cur is not None and cur.kind == "para":
            cur.end = e
            continue
        in_list = False
        cur = Block(s, e, "para")
        blocks.append(cur)
    return blocks


def _split_long(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Cut [start, end) into pieces ≤ max_chars at sentence or line ends, else at whitespace."""
    out = []
    while end - start > max_chars:
        cut = None
        for m in SENTENCE_END.finditer(text, start, start + max_chars):
            if m.start() > start:
                cut = m.start()
        if cut is None:
            ws = text.rfind(" ", start, start + max_chars)
            cut = ws if ws > start else start + max_chars
        out.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if start < end:
        out.append((start, end))
    return out


def segment(text: str, cfg: WindowConfig) -> list[tuple[int, int, list[int]]]:
    """(start, end, block indices) per segment of one turn's text."""
    blocks = parse_blocks(text)
    if not blocks:
        return []
    if blocks[-1].end - blocks[0].start <= cfg.max_chars:
        return [(blocks[0].start, blocks[-1].end, list(range(len(blocks))))]
    segs: list[tuple[int, int, list[int]]] = []
    group: list[int] = []
    for i, b in enumerate(blocks):
        if b.end - b.start > cfg.max_chars:
            if group:
                segs.append((blocks[group[0]].start, blocks[group[-1]].end, group))
                group = []
            for s, e in _split_long(text, b.start, b.end, cfg.max_chars):
                segs.append((s, e, [i]))
            continue
        if group and b.end - blocks[group[0]].start > cfg.max_chars:
            segs.append((blocks[group[0]].start, blocks[group[-1]].end, group))
            keep = [j for j in group[-cfg.overlap_blocks:] if cfg.overlap_blocks > 0]
            if keep and b.end - blocks[keep[0]].start <= cfg.max_chars and len(keep) < len(group):
                group = keep
            else:
                group = []
        group.append(i)
    if group:
        segs.append((blocks[group[0]].start, blocks[group[-1]].end, group))
    return segs


def _tail_start(text: str, chars: int) -> int:
    """Start of the last ``chars`` characters of ``text``, moved forward to a line or sentence start."""
    if len(text) <= chars:
        return 0
    start = len(text) - chars
    m = re.search(r"(?<=[.!?\n])\s*\S", text[start:])
    return start + m.end() - 1 if m else start


def build_windows(content: SessionContent, cfg: WindowConfig) -> list[Window]:
    windows: list[Window] = []
    turns = content.turns
    for t in turns:
        if not t.content.strip():
            continue
        blocks = parse_blocks(t.content)
        segs = segment(t.content, cfg)
        prev = turns[t.index - 1] if t.index > 0 else None
        for n, (s, e, idx) in enumerate(segs):
            spans: list[Span] = []
            context_text = ""
            items = tuple(ListItem(f"{t.index}.{blocks[i].list_no}", blocks[i].ordinal, blocks[i].numbered)
                          for i in idx if blocks[i].kind == "item")
            leads = sorted({blocks[i].lead_in for i in idx if blocks[i].lead_in is not None and blocks[i].lead_in not in idx})
            for li in leads:
                lb = blocks[li]
                spans.append(Span(t.index, lb.start, lb.end, context=True))
                context_text += t.content[lb.start:lb.end] + "\n"
            kind = "turn" if len(segs) == 1 else "segment"
            if (t.role == "user" and len(t.content.strip()) < cfg.short_reply_chars and prev is not None
                    and prev.role == "assistant" and prev.content.strip()):
                cs = _tail_start(prev.content, cfg.context_chars)
                spans.insert(0, Span(prev.index, cs, len(prev.content), context=True))
                context_text = prev.content[cs:] + "\n" + context_text
                kind = "reply"
            spans.append(Span(t.index, s, e))
            prefix = ""
            if t.role == "assistant" and prev is not None and prev.role == "user" and cfg.prompt_prefix_chars > 0:
                prefix = prev.content[: cfg.prompt_prefix_chars] + "\n"
            windows.append(Window(
                window_id=f"{content.session_id}:{t.index}:{n}", session_id=content.session_id, role=t.role,
                spans=tuple(spans), index_text=prefix + context_text + t.content[s:e], list_items=items, kind=kind))
    return windows


def merge_spans(spans: list[Span]) -> list[Span]:
    """Union of overlapping or touching spans per turn, in source order; context only where nothing matched."""
    by_turn: dict[int, list[Span]] = {}
    for sp in spans:
        by_turn.setdefault(sp.turn_index, []).append(sp)
    out: list[Span] = []
    for ti in sorted(by_turn):
        cur: list[Span] = []
        for sp in sorted(by_turn[ti], key=lambda x: (x.start, x.end)):
            if cur and sp.start <= cur[-1].end + 1:
                last = cur[-1]
                cur[-1] = Span(ti, last.start, max(last.end, sp.end), last.context and sp.context)
            else:
                cur.append(sp)
        out += cur
    return out


def render(content: SessionContent, spans: list[Span]) -> str:
    """Original text of ``spans`` with speaker labels; "[…]" marks skipped text between excerpts."""
    lines: list[str] = []
    prev_turn = None
    merged = merge_spans(spans)
    by_turn: dict[int, list[Span]] = {}
    for sp in merged:
        by_turn.setdefault(sp.turn_index, []).append(sp)
    for ti in sorted(by_turn):
        turn = content.turns[ti]
        if prev_turn is not None and ti != prev_turn + 1:
            lines.append("[…]")
        pieces = [turn.content[sp.start:sp.end].strip() for sp in by_turn[ti]]
        head = "" if by_turn[ti][0].start == 0 or not turn.content[: by_turn[ti][0].start].strip() else "[…] "
        tail = "" if not turn.content[by_turn[ti][-1].end:].strip() else " […]"
        lines.append(f"{turn.role}: {head}{' […] '.join(p for p in pieces if p)}{tail}")
        prev_turn = ti
    return "\n".join(lines)

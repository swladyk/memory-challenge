"""Query plans: what a question asks for, inferred from its own words only (never from gold types).

Hints are soft signals with a reason each; they steer budgets and optional steps, never filter candidates out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from ..config import Config
from ..data.dates import parse_dataset_date, question_time_range
from ..schemas import Question
from ..text import BOILERPLATE, STOP_WORDS, query_tokens, tokenize

HINT_RULES: dict[str, re.Pattern] = {
    "assistant_recommendation": re.compile(
        r"\b(you|your)\b.{0,60}\b(recommend\w*|suggest\w*|said|told|mention\w*|gave|list\w*|provid\w*|advis\w*)\b|"
        r"\bremind me\b|\bour (previous|last|earlier) (chat|conversation)\b|\bwhat did you\b", re.I),
    "knowledge_update": re.compile(r"\b(current(ly)?|now|still|latest|most recent(ly)?|these days|anymore|at the moment)\b", re.I),
    "temporal": re.compile(r"\b(when|how long|how many (days|weeks|months|years)|before|after|first|ago|since|between|"
                           r"order|earlier|later|what date|which day|how old)\b", re.I),
    "aggregation": re.compile(r"\b(how many|how much|total|in total|combined|altogether|all the|both|sum|average)\b", re.I),
}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
LIST_NOUNS = r"(recommendation|suggestion|tip|item|step|option|idea|point|book|place|thing|one|exercise|recipe|name|" \
             r"restaurant|destination|activity|question|product|resource|show|movie|song|app|tool|strategy)s?"
ORDINAL_RX = re.compile(rf"\b(the )?((?:{'|'.join(ORDINALS)})|\d+(?:st|nd|rd|th)|last)\b\s+(\w+\s+)?{LIST_NOUNS}\b", re.I)
# Phrasings an answer may use for an attribute the question names (used only when expand_attributes is on).
ATTRIBUTE_EXPANSIONS = {
    "old": ["age", "years old", "year-old", "born", "birthday"],
    "age": ["years old", "year-old", "born", "birthday"],
    "how long": ["since", "years", "months", "started", "began"],
    "cost": ["price", "paid", "spent", "dollars", "$"],
    "price": ["cost", "paid", "spent", "dollars"],
    "spend": ["spent", "paid", "cost", "dollars"],
    "far": ["miles", "km", "kilometers", "distance"],
    "distance": ["miles", "km", "kilometers"],
    "weigh": ["pounds", "lbs", "kg", "weight"],
    "weight": ["pounds", "lbs", "kg"],
    "how many times": ["times", "again", "once", "twice"],
}


@dataclass
class Component:
    label: str
    tokens: list[str]


@dataclass
class QueryPlan:
    question_id: str
    history_id: str
    question: str
    question_date: str
    question_dt: datetime | None
    tokens: list[str]
    hints: dict[str, str] = field(default_factory=dict)  # hint → the words that triggered it
    anchors: list[str] = field(default_factory=list)  # names, quoted titles, numbers with units
    components: list[Component] = field(default_factory=list)
    ordinal: int | None = None  # 1-based; -1 for "last"
    time_range: tuple[date, date, str] | None = None
    expansion: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"hints": self.hints, "anchors": self.anchors, "tokens": self.tokens,
                "components": [{"label": c.label, "tokens": c.tokens} for c in self.components],
                "ordinal": self.ordinal, "expansion": self.expansion,
                "time_range": None if not self.time_range else
                [self.time_range[0].isoformat(), self.time_range[1].isoformat(), self.time_range[2]]}


def _anchors(question: str) -> list[str]:
    out = re.findall(r"[\"“']([^\"”']{3,60})[\"”']", question)
    words = re.findall(r"\b[A-Z][a-zA-Z0-9'&.-]+(?:\s+[A-Z][a-zA-Z0-9'&.-]+)*", question)
    first = question.split()[0] if question.split() else ""
    for w in words:
        if w == first or w.lower() in STOP_WORDS or w.lower() in BOILERPLATE or w in ("I",):
            continue
        out.append(w)
    out += re.findall(r"\b\d[\d,.:/]*\s?(?:%|[a-zA-Z]+)?", question)
    return list(dict.fromkeys(a.strip() for a in out if a.strip()))


def _components(question: str, tokens: list[str]) -> list[Component]:
    q = question.strip().rstrip("?")
    m = re.search(r"\bbetween (.+?) and (.+)$", q, re.I)
    parts = [m.group(1), m.group(2)] if m else []
    if not parts and re.search(r"\b(how many|total|combined|both)\b", q, re.I) and " and " in q:
        head, _, tail = q.rpartition(" and ")
        parts = [head.split(" in ", 1)[-1] if " in " in head else head, tail]
    comps = []
    for p in parts:
        toks = tokenize(p, STOP_WORDS | BOILERPLATE)
        if toks:
            comps.append(Component(p.strip(), list(dict.fromkeys(toks))))
    return comps if len(comps) > 1 else [Component(question, tokens)]


def make_plan(q: Question, cfg: Config) -> QueryPlan:
    qdt = parse_dataset_date(q.question_date)
    toks = query_tokens(q.question, cfg.lexical.drop_boilerplate)
    plan = QueryPlan(q.question_id, q.history_id, q.question, q.question_date, qdt, toks)
    for name, rx in HINT_RULES.items():
        m = rx.search(q.question)
        if m:
            plan.hints[name] = m.group(0)
    om = ORDINAL_RX.search(q.question)
    if om:
        word = om.group(2).lower()
        plan.ordinal = -1 if word == "last" else ORDINALS.get(word) or int(re.sub(r"\D", "", word) or 0) or None
        if plan.ordinal:
            plan.hints["ordered_item"] = om.group(0)
    if not plan.hints:
        plan.hints["exact_fact"] = "default"
    plan.anchors = _anchors(q.question)
    plan.components = _components(q.question, toks)
    plan.time_range = question_time_range(q.question, qdt)
    if cfg.lexical.expand_attributes:
        low = q.question.lower()
        for key, extra in ATTRIBUTE_EXPANSIONS.items():
            if re.search(rf"\b{re.escape(key)}", low):
                plan.expansion += [t for phrase in extra for t in tokenize(phrase)]
        plan.expansion = [t for t in dict.fromkeys(plan.expansion) if t not in toks]
    return plan

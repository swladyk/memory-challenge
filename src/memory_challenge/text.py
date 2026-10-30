"""Tokenisation for lexical retrieval: lowercase word/number tokens, English stop words, a light suffix stemmer.

Deterministic and dependency-free. The stemmer only folds common inflections (plurals, -ing, -ed, -ly, final -e) so
that "baking", "baked" and "bake" meet; it is not a full Porter stemmer.
"""
from __future__ import annotations

import re
from functools import lru_cache

TOKEN = re.compile(r"[a-z0-9]+")

STOP_WORDS = frozenset("""
a about above after again against all am an and any are aren't as at be because been before being below between both
but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each few for from further
had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself him himself his how how's i
i'd i'll i'm i've if in into is isn't it it's its itself let's me more most mustn't my myself no nor not of off on
once only or other ought our ours ourselves out over own same shan't she she'd she'll she's should shouldn't so some
such than that that's the their theirs them themselves then there there's these they they'd they'll they're they've
this those through to too under until up very was wasn't we we'd we'll we're we've were weren't what what's when
when's where where's which while who who's whom why why's with won't would wouldn't you you'd you'll you're you've your
yours yourself yourselves also just get got really like would could may might will shall s t d ll m re ve
""".split())

# Words that frame a question about the conversation itself rather than its content.
BOILERPLATE = frozenset("""
remind remember recall previous earlier last chat chats conversation conversations talk talked discuss discussed
mention mentioned told tell said say ask asked wondering
""".split())


@lru_cache(maxsize=200_000)
def stem(word: str) -> str:
    """Fold common English inflections; digits and short words are returned unchanged."""
    w = word
    if len(w) <= 3 or w.isdigit():
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith(("sses", "xes", "zes", "ches", "shes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suffix in ("ingly", "edly", "ing", "ed", "ly"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            if len(w) >= 3 and w[-1] == w[-2] and w[-1] not in "lsz":
                w = w[:-1]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    return w


def tokenize(text: str, drop: frozenset[str] = STOP_WORDS) -> list[str]:
    """Stemmed content tokens of ``text`` (stop words and single letters removed; digits kept)."""
    out = []
    for tok in TOKEN.findall(text.lower()):
        if tok in drop or (len(tok) == 1 and not tok.isdigit()):
            continue
        out.append(stem(tok))
    return out


def query_tokens(question: str, drop_boilerplate: bool = True) -> list[str]:
    """Tokens of a question, de-duplicated in order; optionally without conversation boilerplate words."""
    drop = STOP_WORDS | BOILERPLATE if drop_boilerplate else STOP_WORDS
    return list(dict.fromkeys(tokenize(question, drop)))


def normalize(text: str) -> str:
    """Lowercase alphanumeric normal form used for phrase matching (keeps 27:12, 2.5, 1/48 together)."""
    return " ".join(re.findall(r"[a-z0-9]+(?:[.:/][0-9]+)*", text.lower()))

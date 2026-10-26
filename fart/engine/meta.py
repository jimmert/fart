"""Meta clusters: tokenize names/symbols of recent coins, group by term.

Unigrams of >= min_len characters and lowercase bigrams of adjacent words; stopwords from
config/stopwords.json. Ranking is done by money and distinct wallets (features.py), not by
coin count.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.lower()


def words(text: str | None) -> list[str]:
    # split camelCase / PascalCase before lowering: "PepeTrump" -> "pepe trump"
    if text:
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(text))
    return _WORD.findall(normalize(text))


def tokenize(name: str | None, symbol: str | None, stopwords: set[str], min_len: int = 4) -> set[str]:
    terms: set[str] = set()
    nw = words(name)
    sw = words(symbol)
    for w in nw + sw:
        if len(w) >= min_len and w not in stopwords and not w.isdigit():
            terms.add(w)
    for a, b in zip(nw, nw[1:]):
        if a in stopwords and b in stopwords:
            continue
        if len(a) + len(b) >= min_len and not (a.isdigit() and b.isdigit()):
            terms.add(f"{a} {b}")
    return terms


def cluster(coins: Iterable[dict], stopwords: set[str], min_len: int = 4) -> dict[str, list[dict]]:
    """term -> coins (dicts with at least mint/name/symbol/creator)."""
    out: dict[str, list[dict]] = {}
    for c in coins:
        for t in tokenize(c.get("name"), c.get("symbol"), stopwords, min_len):
            out.setdefault(t, []).append(c)
    return out


def load_stopwords(cfg: dict) -> set[str]:
    return {normalize(w) for w in cfg.get("stopwords", [])}

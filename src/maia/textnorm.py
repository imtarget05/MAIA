"""Vietnamese-friendly text normalization (P1, dependency-free stdlib only).

Used by BM25 tokenization (retriever) and the topical-support check
(evidence gate): Vietnamese queries must match English enterprise docs
through accent-stripped tokens + a small VI→EN synonym map, otherwise
every cross-lingual query looks like "no topical support".
"""
from __future__ import annotations

import re
import unicodedata

# Vietnamese-specific: unicodedata NFD does not decompose đ/Đ.
_D_MAP = str.maketrans({"đ": "d", "Đ": "D"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def strip_accents(text: str) -> str:
    """Lowercase + remove diacritics (tiếng Việt → tieng viet)."""
    text = (text or "").translate(_D_MAP)
    nfkd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def norm_tokens(text: str) -> list[str]:
    """Normalized alphanumeric tokens for overlap/BM25."""
    return _TOKEN_RE.findall(strip_accents(text))


# Minimal VI→EN synonym map for MAIA enterprise domains.
# Query-side expansion only: each Vietnamese token maps to English doc terms.
VI_EN_SYNONYMS: dict[str, list[str]] = {
    "nghi": ["leave"],
    "phep": ["leave"],
    "phepnam": ["leave"],
    "ngay": ["days", "day"],
    "con": ["balance", "remaining"],
    "laptop": ["laptop"],
    "may": ["device", "computer"],
    "tinh": ["device", "computer"],
    "mat": ["lost"],
    "that": ["lost"],
    "lac": ["lost"],
    "danh": ["lost"],
    "hong": ["broken"],
    "hu": ["broken"],
    "khoi": ["boot"],
    "dong": ["boot"],
    "vpn": ["vpn"],
    "mang": ["network", "vpn"],
    "bao": ["security", "insurance"],
    "chinh": ["policy"],
    "sach": ["policy"],
    "quy": ["policy", "procedure"],
    "dinh": ["policy", "procedure"],
    "thu": ["procedure", "process"],
    "tuc": ["procedure", "process"],
    "chi": ["expense", "reimbursement"],
    "phi": ["expense", "fee"],
    "hoan": ["reimbursement"],
    "cong": ["travel", "business", "attendance"],
    "tac": ["travel", "business"],
    "phuc": ["benefits"],
    "loi": ["benefits"],
    "hiem": ["insurance"],
    "gym": ["gym", "wellness"],
    "khoa": ["training"],
    "hoc": ["training", "learning"],
    "nhan": ["employee", "onboarding"],
    "vien": ["employee"],
    "moi": ["onboarding", "new"],
    "dau": ["first", "onboarding"],
    "sso": ["sso"],
    "email": ["email"],
    "luong": ["payroll", "salary"],
    "tre": ["late", "attendance"],
    "som": ["attendance"],
    "cham": ["attendance"],
    "lam": ["work"],
    "viec": ["work"],
    "tu": ["remote"],
    "xa": ["remote"],
    "giup": ["help", "support"],
    "lien": ["contact"],
    "he": ["contact", "support"],
    "bo": ["department", "team"],
    "phan": ["department"],
    "tao": ["create", "request"],
    "yeu": ["request"],
    "cau": ["request"],
    "gui": ["submit", "request"],
    "duyet": ["approval"],
    "phe": ["approval", "leave"],
    # NOTE: keys are accent-stripped single tokens; multi-word matching
    # happens token-by-token in expand_tokens().
}


def expand_tokens(tokens: list[str]) -> set[str]:
    """Query tokens + their EN synonyms (for cross-lingual overlap)."""
    out = set(tokens)
    for t in tokens:
        for syn in VI_EN_SYNONYMS.get(t, ()):
            out.add(syn)
    return out


def topical_overlap(query: str, context: str) -> float:
    """Fraction of (expanded) query tokens present in the context.

    Returns 0..1. Used as the topical-support signal in the evidence gate.
    """
    qtokens = norm_tokens(query)
    if not qtokens:
        return 0.0
    expanded = expand_tokens(qtokens)
    ctx = set(norm_tokens(context))
    if not ctx:
        return 0.0
    return len(expanded & ctx) / max(1, len(expanded))

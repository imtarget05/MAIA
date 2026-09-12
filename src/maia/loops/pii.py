"""G-04: PII detection + redaction for the MAIA knowledge base.

Two layers (defense-in-depth, same spirit as G-01):
  1. Ingestion-time (pipeline_query._ingest_rawdocs): scan + redact chunk text
     before it is embedded/stored — a document containing PII is never indexed
     verbatim.
  2. Generation-time (OutputGuardrail): catch PII that slips through ingestion
     (e.g. PII typed directly into a query that gets echoed back in the answer).

Conservative scope (extend patterns later): email, VN phone, CCCD/CMND, credit card.
Deliberately NOT US SSN — this system serves vi_policy queries, so a US-SSN
pattern would pass its acceptance criteria while catching no real PII in the data.

G-04-FU2: internal role/department emails (it-help@, hr@, ... @company.com,
see ROLE_EMAIL_* in config) are public contact info — they bypass EMAIL
detection so "who do I contact?" answers stay usable. Personal emails are
still always redacted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- patterns (order matters: more specific first) ---
# CCCD/CMND: 12 digits (CCCD) or 9 digits (CMND), optionally space/hyphen-separated.
_CCCD = re.compile(r"\b(?:\d{3}[ -]?\d{3}[ -]?\d{3}[ -]?\d{3}|\d{3}[ -]?\d{3}[ -]?\d{3})\b")
# VN phone: 0 or +84 followed by 9-10 digits (common carriers: 09x/03x/07x/08x/05x).
_VN_PHONE = re.compile(r"(?:\+?84|0)(?:3[2-9]|5[6-9]|7[06-9]|8[1-9]|9[0-9])\d{7}\b")
# Credit card: 13-19 digits, space/hyphen-separated; light Luhn sanity in match().
_CC = re.compile(r"\b(?:\d{4}[ -]?){3,4}\d{1,4}\b")
# Email.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# --- G-04-FU2: role-email allowlist -------------------------------------------
# Internal role/department emails (it-help@, hr@, ...) are public contact info,
# NOT personal PII. They must survive detect()/redact() so "who do I contact?"
# answers stay usable. Personal emails keep the old behavior (always redact).
# Spec: docs/G-04-FU2_PII_ROLE_ALLOWLIST.md
_ROLE_EMAIL_EXACT_DEFAULTS = frozenset({
    "support@company.com", "help@company.com", "it-help@company.com",
    "hr@company.com", "hr-escalation@company.com", "security@company.com",
    "benefits@company.com", "eap@company.com", "finance@company.com",
    "onboarding@company.com",
})
_ROLE_PREFIX_DEFAULTS = (
    "support", "help", "it-help", "it-", "hr", "hr-",
    "security", "benefits", "eap", "onboarding", "finance",
)
_ROLE_DOMAIN_DEFAULT = "company.com"


def _load_role_allowlist() -> tuple[frozenset, tuple, str]:
    """Exact role emails + allowed local-part prefixes + domain.

    Reads ROLE_EMAIL_* from settings (env-overridable); falls back to the
    module defaults when settings are unavailable (e.g. import cycles).
    All values normalized to lowercase.
    """
    try:
        from ..config import settings as _s

        exact = frozenset(
            e.strip().lower()
            for e in str(getattr(_s, "ROLE_EMAIL_ALLOWLIST", "") or "").split(",")
            if e.strip()
        ) or _ROLE_EMAIL_EXACT_DEFAULTS
        prefixes = tuple(
            p.strip().lower()
            for p in str(getattr(_s, "ROLE_EMAIL_ALLOW_PREFIXES", "") or "").split(",")
            if p.strip()
        ) or _ROLE_PREFIX_DEFAULTS
        domain = str(
            getattr(_s, "ROLE_EMAIL_ALLOW_DOMAIN", "") or _ROLE_DOMAIN_DEFAULT
        ).strip().lower()
        return exact, prefixes, domain
    except Exception:
        return _ROLE_EMAIL_EXACT_DEFAULTS, _ROLE_PREFIX_DEFAULTS, _ROLE_DOMAIN_DEFAULT


def is_role_email(email: str, exact: frozenset | set | None = None) -> bool:
    """True if an email address is an internal role/department contact.

    `exact` overrides the configured exact set (used by tests); prefix/domain
    rules always come from config/defaults.
    """
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return False
    if exact is None:
        exact, prefixes, domain = _load_role_allowlist()
    else:
        _, prefixes, domain = _load_role_allowlist()
        exact = frozenset(e.lower() for e in exact)
    if addr in exact:
        return True
    local, _, dom = addr.partition("@")
    return dom == domain and local.startswith(prefixes)

_PII_RULES = [
    ("CCCD", _CCCD),
    ("PHONE", _VN_PHONE),
    ("CREDIT_CARD", _CC),
    ("EMAIL", _EMAIL),
]


@dataclass
class PIIMatch:
    pii_type: str
    span: tuple[int, int]


def _luhn_ok(num: str) -> bool:
    digits = [int(d) for d in num if d.isdigit()]
    if len(digits) < 13:
        return False
    s = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return s % 10 == 0


class PIIScanner:
    """Detect + redact PII patterns in text.

    G-04-FU2: EMAIL matches that are internal role contacts (see is_role_email)
    are skipped — they are public contact info, not personal PII.
    """

    @staticmethod
    def detect(text: str, role_allowlist: frozenset | set | None = None) -> list[PIIMatch]:
        hits: list[PIIMatch] = []
        for pii_type, pat in _PII_RULES:
            for m in pat.finditer(text):
                if pii_type == "CREDIT_CARD" and not _luhn_ok(m.group(0)):
                    continue
                if pii_type == "EMAIL" and is_role_email(m.group(0), exact=role_allowlist):
                    continue
                hits.append(PIIMatch(pii_type, m.span()))
        # de-overlap: keep longest span per region (greedy by start then length)
        hits.sort(key=lambda h: (h.span[0], -(h.span[1] - h.span[0])))
        deduped: list[PIIMatch] = []
        last_end = -1
        for h in hits:
            if h.span[0] >= last_end:
                deduped.append(h)
                last_end = h.span[1]
        return deduped

    @staticmethod
    def is_dirty(text: str, role_allowlist: frozenset | set | None = None) -> bool:
        return len(PIIScanner.detect(text, role_allowlist)) > 0

    @staticmethod
    def redact(text: str, role_allowlist: frozenset | set | None = None) -> str:
        """Replace each PII span with [PII-<TYPE>] (right-to-left to keep spans valid)."""
        hits = PIIScanner.detect(text, role_allowlist)
        if not hits:
            return text
        parts: list[str] = []
        prev = 0
        for h in sorted(hits, key=lambda x: x.span[0]):
            parts.append(text[prev:h.span[0]])
            parts.append(f"[PII-{h.pii_type}]")
            prev = h.span[1]
        parts.append(text[prev:])
        return "".join(parts)

"""Loop 3 - Answer Quality Loop.

Retrieved context -> prompt -> LLM -> answer -> grounding/citation check.

If the generated answer cannot be grounded in the retrieved context (or cites a
source that does not exist), DO NOT hallucinate: retry with a stricter prompt, and
if it still fails, fall back to an explicit "I don't have enough evidence..."
response.

Components:
  * GroundingChecker - is each answer claim supported by the context?
  * CitationChecker  - do all [Sn] tags map to a real retrieved chunk?
"""
from __future__ import annotations

import re
from collections.abc import Callable

from ..config import settings
from ..prompt import assemble, build_messages

GROUNDING_THRESHOLD = settings.AGENT_GROUNDING_THRESHOLD

FALLBACK_TEXT = (
    "I don't have enough evidence in the knowledge base to answer this reliably, "
    "so I won't guess. Please rephrase or add the relevant document to MAIA."
)

_CITE_RE = re.compile(r"\[S(\d+)\]")


def extract_cites(answer: str) -> list[int]:
    """All [Sn] citation indices referenced by the answer."""
    return sorted({int(n) for n in _CITE_RE.findall(answer)})


# Grounding reason codes surfaced to callers / UI.
GROUNDED = "SUPPORTED"
NOT_GROUNDED = "NOT_SUPPORTED"
PARTIAL = "PARTIAL"
UNVERIFIABLE = "UNVERIFIABLE"
TOKENS_LOW = "TOKENS_BELOW_THRESHOLD"


class CitationChecker:
    """Verifies every [Sn] in the answer maps to a real, returned chunk."""

    def __init__(self, candidates: list[dict]) -> None:
        # candidate rank (1-based) -> chunk_id that the prompt presents as [S#]
        self._available = {i + 1: c["chunk_id"] for i, c in enumerate(candidates)}
        self.real_cites = [c["chunk_id"] for c in candidates]
        # rank -> chunk text (for support heuristic); empty string if absent
        self._texts = {i + 1: c.get("text", "") for i, c in enumerate(candidates)}
        # rank -> tenant_id of the cited chunk (for cross-tenant cite defense)
        self._tenants = {i + 1: c.get("metadata", {}).get("tenant_id", "")
                         for i, c in enumerate(candidates)}

    def check(self, answer: str) -> tuple[bool, list[int]]:
        """Return (valid, list_of_invalid_cite_ranks)."""
        invalid = [n for n in extract_cites(answer) if n not in self._available]
        return (len(invalid) == 0), invalid

    def check_reasons(self, answer: str) -> tuple[bool, dict[int, str]]:
        """Like check() but returns a per-rank reason map for invalid cites."""
        reasons: dict[int, str] = {}
        for n in extract_cites(answer):
            if n not in self._available:
                reasons[n] = "missing"
            elif not self._texts.get(n):
                reasons[n] = "no_text"
        return (len(reasons) == 0), reasons

    def verify_support(self, answer: str, cite_tag: int | None = None) -> bool:
        """Heuristic: the sentence containing [S<cite_tag>] shares tokens with
        the cited chunk's text. Returns True if supported (or indeterminate)."""
        from ..textnorm import norm_tokens
        tag = cite_tag if cite_tag is not None else None
        if tag is None:
            return True
        chunk_text = self._texts.get(tag, "")
        if not chunk_text:
            return False
        import re as _re
        needle = f"[S{tag}]"
        idx = answer.find(needle)
        if idx == -1:
            return False
        # sentence start = char after the last sentence-ending punctuation
        last_period = answer.rfind(".", 0, idx)
        start = (last_period + 1) if last_period != -1 else 0
        # sentence end = next sentence-ending punctuation after the tag
        sentence_end = len(answer)
        m = _re.search(r"[.!?]", answer[idx:])
        if m:
            sentence_end = idx + m.start()
        sentence = answer[start:sentence_end].strip()
        q_tokens = set(norm_tokens(sentence))
        if not q_tokens:
            return True
        c_tokens = set(norm_tokens(chunk_text))
        overlap = len(q_tokens & c_tokens) / len(q_tokens)
        return overlap >= 0.1

    def check_unauthorized_sources(self, answer: str, allowed_tenants: set[str]) -> list[int]:
        """Return ranks of cites whose chunk tenant is not in allowed_tenants.
        Defensive: citations should only reference same-tenant chunks because
        retrieval is already tenant-filtered."""
        bad = []
        for n in extract_cites(answer):
            t = self._tenants.get(n, "")
            if t and allowed_tenants and t not in allowed_tenants:
                bad.append(n)
        return bad


class GroundingChecker:
    """Checks the answer stays within the provided context (grounded, no hallucination)."""

    def __init__(self, threshold: float = GROUNDING_THRESHOLD) -> None:
        self.threshold = threshold

    @staticmethod
    def _overlap(text_a: str, text_b: str) -> float:
        sa = set(text_a.lower().split())
        sb = set(text_b.lower().split())
        if not sa:
            return 0.0
        return len(sa & sb) / len(sa)

    def score(self, answer: str, context: str) -> float:
        """Fraction of answer tokens present in the context (0..1)."""
        return self._overlap(answer, context)

    def grounded(self, answer: str, context: str) -> bool:
        return self.score(answer, context) >= self.threshold

    def check(self, answer: str, context: str) -> tuple[bool, float]:
        s = self.score(answer, context)
        return (s >= self.threshold), s

    def check_detailed(self, answer: str, context: str) -> tuple[bool, float, str]:
        """Return (supported, score, reason_code)."""
        s = self.score(answer, context)
        if s >= self.threshold:
            return True, s, GROUNDED
        if not context.strip():
            return False, 0.0, UNVERIFIABLE
        return False, s, TOKENS_LOW


class LLMGroundingChecker:
    """LLM-as-judge grounding checker.

    Offline-safe: when ``USE_LLM_GROUNDING`` is False (the default, enforced by
    conftest for all tests) or the LLM is unavailable / returns malformed
    output, it transparently falls back to token-overlap GroundingChecker.

    The ``check`` interface matches GroundingChecker: returns
    ``(supported, score)``. Use ``check_detailed`` for a reason code.
    """

    def __init__(self, llm=None, threshold: float = settings.AGENT_GROUNDING_THRESHOLD,
                 fallback_threshold: float = settings.AGENT_GROUNDING_THRESHOLD) -> None:
        self.llm = llm
        self.threshold = threshold
        self.fallback = GroundingChecker(threshold=fallback_threshold)
        self.mode = "llm" if (settings.USE_LLM_GROUNDING and llm is not None) else "token"

    def _judge(self, answer: str, context: str) -> tuple[bool, float, str] | None:
        """Call the LLM to judge per-claim support. Returns (supported, score, reason)
        or None if the LLM call/output fails (→ caller falls back to token overlap)."""
        prompt = (
            "For each factual claim in the answer, decide whether the CONTEXT "
            "supports it. Output ONLY a JSON array, one object per claim:\n"
            "[{'claim': <str>, 'supported': true|false}]. Overall SUPPORTED=true "
            "only if ALL claims are supported. If you cannot parse claims or the "
            "context is empty, output an empty array.\n\n"
            f"CONTEXT:\n{context}\n\nANSWER:\n{answer}\n"
        )
        try:
            raw = self.llm.chat([{"role": "user", "content": prompt}])
            import json as _json
            data = _json.loads(raw.replace("```json", "").replace("```", "").strip())
            claims = data if isinstance(data, list) else []
            if not claims:
                return False, 0.0, UNVERIFIABLE
            supported_count = sum(1 for c in claims if isinstance(c, dict) and c.get("supported"))
            score = supported_count / len(claims)
            return score == 1.0, score, GROUNDED if score == 1.0 else PARTIAL
        except Exception:
            # Malformed LLM output or unavailable → fall back to token overlap.
            return None

    def check(self, answer: str, context: str) -> tuple[bool, float]:
        if self.mode != "llm":
            return self.fallback.check(answer, context)
        judged = self._judge(answer, context)
        if judged is not None:
            return judged[0], judged[1]
        return self.fallback.check(answer, context)

    def check_detailed(self, answer: str, context: str) -> tuple[bool, float, str]:
        if self.mode != "llm":
            return self.fallback.check_detailed(answer, context)
        judged = self._judge(answer, context)
        if judged is not None:
            return judged
        return self.fallback.check_detailed(answer, context)

def guarded_generate(question: str, candidates: list[dict], llm: Callable,
                      max_chars: int = 3000, max_retries: int = 2,
                      threshold: float = settings.AGENT_GROUNDING_THRESHOLD) -> dict:
    """Loop 3 orchestration: build context, generate, verify, retry/ fallback.

    Returns a result dict with `answer`, `has_evidence`, `citations`, and the
    verification details (grounding_score, cites_valid).
    """
    context, used = assemble(candidates, max_chars=max_chars)
    if not used:
        return {"answer": FALLBACK_TEXT, "has_evidence": False, "citations": [],
                "grounding_score": 0.0, "cites_valid": True, "retries": 0,
                "fallback": True}

    messages = build_messages(question, context)
    citation_checker = CitationChecker(used)
    grounding_checker = GroundingChecker(threshold=threshold)

    answer = ""
    retries = 0
    grounding_score, cites_valid = 0.0, True
    for attempt in range(max_retries + 1):
        answer = llm(messages)
        grounding_score = grounding_checker.score(answer, context)
        cites_valid, _invalid = citation_checker.check(answer)
        ok = grounding_checker.grounded(answer, context) and cites_valid
        if ok:
            break
        retries = attempt + 1
        # tighten the prompt to force a grounded, citation-only answer
        messages = build_messages(
            question,
            context + "\n\n[RULE] Answer ONLY using [S#] tags above. If unsure, "
            "reply exactly: \"" + FALLBACK_TEXT + "\"")
    else:
        answer = FALLBACK_TEXT
        grounding_score = 0.0
        cites_valid = True  # fallback cites nothing

    return {
        "answer": answer, "has_evidence": bool(used), "citations": used,
        "grounding_score": round(grounding_score, 4),
        "cites_valid": bool(cites_valid), "retries": retries,
        "fallback": answer == FALLBACK_TEXT,
    }


"""Loop 3 - Answer Quality Loop.

Retrieved context -> prompt -> LLM -> answer -> grounding/citation check.

If the generated answer cannot be grounded in the retrieved context (or cites a
source that does not exist), DO NOT hallucinate: retry with a stricter prompt, and
if it still fails, fall back to an explicit "I don't have enough evidence..."
response.

Components:
  * GroundingChecker - is each answer claim supported by the context?
  * CitationChecker  - do all [Sn] tags map to a real retrieved chunk?
  * guarded_generate - assemble -> LLM -> check -> retry/fallback
"""
from __future__ import annotations

import re
from typing import Callable

from ..prompt import assemble, build_messages

# Overlap threshold used as a *lightweight grounding proxy* (exact grounding via
# an LLM judge is a drop-in upgrade; this keeps the loop offline-testable).
GROUNDING_THRESHOLD = 0.15
FALLBACK_TEXT = (
    "I don't have enough evidence in the knowledge base to answer this reliably, "
    "so I won't guess. Please rephrase or add the relevant document to MAIA."
)

_CITE_RE = re.compile(r"\[S(\d+)\]")


def extract_cites(answer: str) -> list[int]:
    """All [Sn] citation indices referenced by the answer."""
    return sorted({int(n) for n in _CITE_RE.findall(answer)})


class CitationChecker:
    """Verifies every [Sn] in the answer maps to a real, returned chunk."""

    def __init__(self, candidates: list[dict]) -> None:
        # candidate rank (1-based) -> chunk_id that the prompt presents as [S#]
        self._available = {i + 1: c["chunk_id"] for i, c in enumerate(candidates)}
        self.real_cites = [c["chunk_id"] for c in candidates]

    def check(self, answer: str) -> tuple[bool, list[int]]:
        """Return (valid, list_of_invalid_cite_ranks)."""
        invalid = [n for n in extract_cites(answer) if n not in self._available]
        return (len(invalid) == 0), invalid


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


def guarded_generate(question: str, candidates: list[dict], llm: Callable,
                     max_chars: int = 3000, max_retries: int = 2,
                     threshold: float = GROUNDING_THRESHOLD) -> dict:
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
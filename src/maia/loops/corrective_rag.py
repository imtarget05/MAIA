"""Corrective RAG (CRAG) — retrieval grading, refinement, and web fallback.

Pipeline:
    retrieve -> grade each chunk (CORRECT / INCORRECT / AMBIGUOUS)
        -> enough CORRECT  => assemble ("correct")
        -> all INCORRECT   => rewrite query + retry (up to CRAG_MAX_CORRECTIONS)
        -> has AMBIGUOUS   => knowledge refinement (strip off-topic
                             sentences) + re-grade ("refined")
        -> exhausted       => optional web fallback (behind
                             CRAG_WEB_SEARCH_ENABLED, default OFF), else
                             assemble best-effort ("exhausted")

Opt-in via CRAG_ENABLED (default false): zero behavior change unless enabled.
Fully offline-capable: heuristic grader + stdlib web scrape, no new deps.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..config import settings
from ..llm import CloudflareLLM
from ..textnorm import expand_tokens, norm_tokens, topical_overlap


class GradeLabel(Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    AMBIGUOUS = "ambiguous"


@dataclass
class GradeResult:
    chunk_id: str
    label: GradeLabel
    confidence: float
    reason: str


@dataclass
class CorrectiveResult:
    context: str
    used: list[dict]
    action: str  # "correct" | "refined" | "web_fallback" | "exhausted"
    grades: list[GradeResult] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    attempts: int = 1
    fallback_answer: str | None = None


class RetrievalGrader:
    """Grade each retrieved chunk for relevance to the query.

    Heuristic (default, no LLM): dense score + BM25/topical support, using the
    same normalization as the evidence gate (maia.textnorm).
    LLM grader: small structured prompt via CloudflareLLM (MOCK-safe).
    """

    def __init__(self, llm: CloudflareLLM | None = None, use_llm: bool | None = None,
                 threshold: float | None = None):
        self.llm = llm
        self.use_llm = use_llm if use_llm is not None else settings.CRAG_GRADE_USE_LLM
        self.threshold = threshold if threshold is not None else settings.CRAG_GRADE_THRESHOLD

    def grade(self, query: str, chunks: list[dict]) -> list[GradeResult]:
        """Grade each chunk: CORRECT / INCORRECT / AMBIGUOUS."""
        results: list[GradeResult] = []
        for c in chunks:
            if self.use_llm and self.llm is not None:
                results.append(self._grade_llm(query, c))
            else:
                results.append(self._grade_heuristic(query, c))
        return results

    def _grade_heuristic(self, query: str, chunk: dict) -> GradeResult:
        """Fast heuristic: dense score + topical support."""
        dense = float(chunk.get("dense_score", chunk.get("rerank_score", 0.0)) or 0.0)
        bm25 = float(chunk.get("bm25_score", 0.0) or 0.0) > 0
        topical = topical_overlap(query, chunk.get("text", ""))
        cid = chunk.get("chunk_id", "")

        if dense >= self.threshold and (bm25 or topical >= 0.05):
            return GradeResult(cid, GradeLabel.CORRECT, min(1.0, (dense + topical) / 2),
                               f"dense={dense:.3f} topical={topical:.3f} bm25={bm25}")
        if dense < self.threshold * 0.5 and not bm25 and topical < 0.02:
            return GradeResult(cid, GradeLabel.INCORRECT, 1.0 - min(1.0, dense + topical),
                               f"low dense={dense:.3f}, topical={topical:.3f}")
        return GradeResult(cid, GradeLabel.AMBIGUOUS, 0.5,
                           f"borderline dense={dense:.3f} topical={topical:.3f} bm25={bm25}")

    def _grade_llm(self, query: str, chunk: dict) -> GradeResult:
        """LLM-based grading with structured output (falls back to heuristic)."""
        text = (chunk.get("text", "") or "")[:1000]
        try:
            resp = self.llm.chat([
                {"role": "system",
                 "content": "Output exactly one label: CORRECT, INCORRECT, or AMBIGUOUS"},
                {"role": "user",
                 "content": ("You are a retrieval grader. Classify the CHUNK vs the QUERY:\n"
                             "- CORRECT: directly answers or strongly supports the query\n"
                             "- INCORRECT: irrelevant, off-topic\n"
                             "- AMBIGUOUS: partially relevant but insufficient alone\n\n"
                             f"QUERY: {query}\n\nCHUNK: {text}")},
            ], max_tokens=10, temperature=0.0)
            label_str = (resp or "").strip().upper()
            if "INCORRECT" in label_str:
                label = GradeLabel.INCORRECT
            elif "AMBIGUOUS" in label_str:
                label = GradeLabel.AMBIGUOUS
            else:
                label = GradeLabel.CORRECT
            return GradeResult(chunk.get("chunk_id", ""), label,
                               0.8 if label != GradeLabel.AMBIGUOUS else 0.5,
                               f"llm:{label.value}")
        except Exception:
            return self._grade_heuristic(query, chunk)


class KnowledgeRefiner:
    """Refine chunks by stripping sentences with no topical overlap (no LLM)."""

    def refine(self, query: str, chunks: list[dict]) -> list[dict]:
        """Keep only sentences sharing (expanded) tokens with the query."""
        qtokens = expand_tokens(norm_tokens(query))
        if not qtokens:
            return chunks
        refined: list[dict] = []
        for c in chunks:
            text = c.get("text", "") or ""
            sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
            kept = [s for s in sentences if set(norm_tokens(s)) & qtokens]
            if kept:
                refined.append({**c, "text": ". ".join(kept) + "."})
            else:
                refined.append(c)  # never lose the chunk entirely
        return refined


class WebSearchFallback:
    """Optional web fallback. No-op unless CRAG_WEB_SEARCH_ENABLED=true.

    Stdlib-only DuckDuckGo HTML scrape (no API key). Results are marked
    source="web" so answers citing them stay distinguishable from internal
    knowledge. Enterprise default stays OFF: internal questions must refuse
    rather than pull the public internet.
    """

    def __init__(self, enabled: bool | None = None, max_results: int = 3):
        self.enabled = enabled if enabled is not None else settings.CRAG_WEB_SEARCH_ENABLED
        self.max_results = max_results
        self._cache: dict[str, list[dict]] = {}

    def search(self, query: str) -> list[dict]:
        """Return [{title, url, snippet}] or []."""
        if not self.enabled:
            return []
        if query in self._cache:
            return self._cache[query]
        try:
            results = self._duckduckgo_html(query)
        except Exception:
            results = []
        self._cache[query] = results
        return results

    def _duckduckgo_html(self, query: str) -> list[dict]:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "MAIA-CRAG/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        results: list[dict] = []
        for m in re.finditer(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                html, re.DOTALL):
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            if title or snippet:
                results.append({"title": title or m.group(1), "url": m.group(1),
                                "snippet": snippet, "source": "web"})
            if len(results) >= self.max_results:
                break
        return results


class CorrectiveRetriever:
    """Orchestrate the CRAG loop: retrieve -> grade -> correct/refine/fallback."""

    def __init__(self, retriever: Any, reranker: Any, assemble_fn: Callable,
                 grader: RetrievalGrader | None = None,
                 refiner: KnowledgeRefiner | None = None,
                 web_fallback: WebSearchFallback | None = None,
                 llm: CloudflareLLM | None = None):
        self.retriever = retriever
        self.reranker = reranker
        self.assemble_fn = assemble_fn
        self.grader = grader or RetrievalGrader(llm=llm)
        self.refiner = refiner or KnowledgeRefiner()
        self.web_fallback = web_fallback or WebSearchFallback()
        self.max_corrections = settings.CRAG_MAX_CORRECTIONS

    def _retrieve(self, query: str, tenant_id: str | None,
                    session_id: str | None) -> list[dict]:
        try:
            return self.retriever.retrieve(query, tenant_id=tenant_id,
                                           session_id=session_id)
        except TypeError:
            # minimal/mock retrievers without session scoping
            return self.retriever.retrieve(query, tenant_id=tenant_id)

    def _metric(self, action: str) -> None:
        try:
            from ..loops.metrics import registry
            registry.inc("maia_crag_retrievals_total")
            registry.inc(f"maia_crag_action_{action}")
        except Exception:
            pass

    def retrieve_corrective(self, query: str, tenant_id: str | None = None,
                            top_k_final: int | None = None,
                            session_id: str | None = None) -> CorrectiveResult:
        """Execute the full CRAG pipeline."""
        trace: list[dict] = []
        top_k_final = top_k_final or settings.TOP_K_FINAL
        need = max(1, top_k_final // 2)

        candidates = self._retrieve(query, tenant_id, session_id)
        reranked = self.reranker.rerank(query, candidates, top_k=top_k_final * 2)
        trace.append({"step": "initial_retrieve", "count": len(reranked)})

        for attempt in range(self.max_corrections + 1):
            grades = self.grader.grade(query, reranked)
            trace.append({"step": "grade", "attempt": attempt,
                          "grades": [g.label.value for g in grades]})
            correct = [c for c, g in zip(reranked, grades) if g.label == GradeLabel.CORRECT]
            ambiguous = [c for c, g in zip(reranked, grades) if g.label == GradeLabel.AMBIGUOUS]

            if len(correct) >= need:
                context, used = self.assemble_fn(correct[:top_k_final])
                self._metric("correct")
                return CorrectiveResult(context, used, "correct", grades, trace, attempt + 1)

            if not correct and not ambiguous:
                if attempt < self.max_corrections:
                    query = self._rewrite_query(query)
                    trace.append({"step": "rewrite", "query": query})
                    candidates = self._retrieve(query, tenant_id, session_id)
                    reranked = self.reranker.rerank(query, candidates, top_k=top_k_final * 2)
                    continue
                web_results = self.web_fallback.search(query)
                if web_results:
                    context, used = self._assemble_web(web_results, top_k_final)
                    trace.append({"step": "web_fallback", "results": len(web_results)})
                    self._metric("web_fallback")
                    return CorrectiveResult(
                        context, used, "web_fallback", grades, trace, attempt + 1,
                        fallback_answer=f"Web search found {len(web_results)} results for: {query}")
                context, used = self.assemble_fn(reranked[:top_k_final])
                self._metric("exhausted")
                return CorrectiveResult(context, used, "exhausted", grades, trace, attempt + 1)

            if ambiguous and attempt < self.max_corrections:
                refined = self.refiner.refine(query, ambiguous)
                refined_grades = self.grader.grade(query, refined)
                refined_correct = [c for c, g in zip(refined, refined_grades)
                                   if g.label == GradeLabel.CORRECT]
                trace.append({"step": "refine", "refined_correct": len(refined_correct)})
                if len(correct) + len(refined_correct) >= need:
                    context, used = self.assemble_fn((correct + refined_correct)[:top_k_final])
                    self._metric("refined")
                    return CorrectiveResult(context, used, "refined",
                                            grades + refined_grades, trace, attempt + 1)
                reranked = correct + refined
                continue

            context, used = self.assemble_fn(reranked[:top_k_final])
            self._metric("exhausted")
            return CorrectiveResult(context, used, "exhausted", grades, trace, attempt + 1)

        context, used = self.assemble_fn(reranked[:top_k_final])
        self._metric("exhausted")
        return CorrectiveResult(context, used, "exhausted", [], trace, self.max_corrections + 1)

    def _rewrite_query(self, query: str) -> str:
        """Heuristic rewrite: append the first intent hint not already present."""
        from ..agent.agent import _INTENT_HINTS  # deferred: avoids import cycle
        for hints in _INTENT_HINTS.values():
            for h in hints:
                if h.lower() not in query.lower():
                    return f"{query} {h}"
        return query + " policy procedure"

    def _assemble_web(self, web_results: list[dict], top_k: int) -> tuple[str, list[dict]]:
        """Assemble web results with boundary tags, marked source=web.

        P1-2: web snippets are UNTRUSTED external data — run DocumentSanitizer
        on each snippet BEFORE wrapping in boundary tags so injected directives
        cannot be re-interpreted as instructions.
        """
        from ..prompt import DOCUMENT_CLOSE_TAG, DOCUMENT_OPEN_TAG
        from .guardrails import DocumentSanitizer
        _sanitizer = DocumentSanitizer()
        parts: list[str] = []
        used: list[dict] = []
        for i, r in enumerate(web_results[:top_k]):
            tag = f"[S{i + 1}]"
            snippet, _dirty = _sanitizer.sanitize(r["snippet"])
            parts.append(
                f"{tag} {DOCUMENT_OPEN_TAG.format(chunk_id=f'web_{i}', filename=r['title'])}\n"
                f"{snippet}\n{DOCUMENT_CLOSE_TAG}")
            used.append({"chunk_id": f"web_{i}", "text": snippet,
                         "metadata": {"filename": r["title"], "source": "web"},
                         "cite_tag": tag, "dense_score": 0.5, "rerank_score": 0.5})
        return "\n\n---\n\n".join(parts), used

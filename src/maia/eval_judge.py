"""Optional LLM-judge for answer faithfulness (P3).

Off by default: the offline pipeline reports `faithfulness_proxy` (keyword) +
`faithfulness_embed` (cosine). Set MAIA_EVAL_JUDGE=1 with a real LLM backend
(`LLM_PROVIDER=local|cloudflare` + creds/reachable gateway) to add
`faith_judge` (0.0-1.0) per row. In mock mode the judge returns None (honest
no-signal) instead of a fake score.
"""
from __future__ import annotations

import json
import os

_JUDGE_PROMPT = """You judge whether the ANSWER is fully supported by the CONTEXT.
Reply with exactly one JSON object: {{"supported": <0.0-1.0>, "reason": "<short>"}}.
Score 1.0 = every factual claim appears in CONTEXT. Score 0.0 = contradicts or invents facts.
No markdown, no extra text.

CONTEXT:
{context}

ANSWER:
{answer}
"""


def judge_faithfulness(answer: str, ctx_chunks: list[str], llm=None) -> dict | None:
    ctx = "\n---\n".join([c for c in ctx_chunks if c and c.strip()])[:6000]
    if not (answer or "").strip() or not ctx.strip():
        return None
    if llm is None:
        from maia.llm import build_llm

        llm = build_llm()
    if getattr(llm, "mode", "") == "mock":
        return None  # no real generation -> no judge signal
    try:
        raw = llm.chat([{"role": "user",
                          "content": _JUDGE_PROMPT.format(context=ctx, answer=answer[:2000])}],
                       max_tokens=256, temperature=0.0)
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(raw[start:end + 1])
        score = float(data.get("supported", -1))
        if not 0.0 <= score <= 1.0:
            return None
        return {"faith_judge": round(score, 3), "reason": str(data.get("reason", ""))[:200]}
    except Exception:
        return None


def judge_enabled() -> bool:
    return os.environ.get("MAIA_EVAL_JUDGE", "").strip() == "1"

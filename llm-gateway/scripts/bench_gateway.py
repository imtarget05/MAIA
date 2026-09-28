"""Micro-benchmarks for llm-gateway v2 routing/governance hot paths (stdlib only).

Measures mean/p95 latency of:
  - alias_resolution : resolve_model_alias over 10k mixed alias/concrete/auto inputs
  - pii_masking      : _pii_mask_body over 1k bodies (half contain VN phone/CCCD)
  - quota_check      : _quota_check over 10k calls (capped mode)

This is the SSOT benchmark tool (MAIA/llm-gateway is upstream source of truth).
Run: .venv/bin/python scripts/bench_gateway.py
Exit 0 always; prints a timing table (machine-dependent numbers feed
docs/BUSINESS_IMPACT.md as MEASURED evidence, never as production SLOs).
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


def _p95(xs: list[float]) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(0.95 * (len(s) - 1)))]


def _bench(n: int, fn) -> tuple[float, float]:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return sum(samples) / len(samples), _p95(samples)


def main() -> int:
    rng = random.Random(20260927)

    aliases = ["fast", "reasoning", "vision", "vision-fast", "embed",
               "general", "long", "auto", "", "custom-model-x", "FAST",
               "qwen2.5-vl-3b-instruct"]
    tasks = ["", "", "auto", "fast", "reasoning", "vision", "embed"]
    alias_inputs = [(rng.choice(aliases), rng.choice(tasks), rng.random() < 0.2)
                    for _ in range(10_000)]

    bodies = []
    for i in range(1_000):
        if i % 2 == 0:
            bodies.append({"model": "fast",
                           "messages": [{"role": "user",
                                         "content": f"xin chào, báo cáo ca {i}"}]})
        else:
            bodies.append({"model": "fast",
                           "messages": [{"role": "user",
                                         "content": f"liên hệ 0901234567, cccd 00123456789{i % 10}"}]})

    # Capped quota mode for a realistic check path (restored afterwards).
    prev_quota = server.QUOTA_TOKENS_PER_DAY
    server.QUOTA_TOKENS_PER_DAY = 1_000_000
    server._reset_quota_state()
    try:
        rows = []
        it = iter(alias_inputs)

        def do_alias() -> None:
            m, t, hi = next(it)
            server.resolve_model_alias(m, task=t, has_image=hi)

        mean, p95 = _bench(10_000, do_alias)
        rows.append(("alias_resolution", 10_000, mean, p95))

        bit = iter(bodies)

        def do_pii() -> None:
            server._pii_mask_body(next(bit))

        mean, p95 = _bench(1_000, do_pii)
        rows.append(("pii_masking", 1_000, mean, p95))

        def do_quota() -> None:
            server._quota_check("bench", 2)

        mean, p95 = _bench(10_000, do_quota)
        rows.append(("quota_check", 10_000, mean, p95))
    finally:
        server.QUOTA_TOKENS_PER_DAY = prev_quota
        server._reset_quota_state()

    print(f"{'op':<18}{'n':>8}{'mean_ms':>12}{'p95_ms':>12}")
    for op, n, mean, p95 in rows:
        print(f"{op:<18}{n:>8}{mean:>12.4f}{p95:>12.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

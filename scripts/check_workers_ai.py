#!/usr/bin/env python3
"""Kiem tra Cloudflare Workers AI connectivity cho MAIA (KHONG R2 storage).

Doc 2 env (KHONG hardcode secret — token chi tu env / Render Dashboard):
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, (optional) CLOUDFLARE_MODEL

Che do:
    Mac dinh (khong --live): KHONG goi mang. Bao cao llm_mode / embed_mode
    tu settings hien tai; thieu creds -> mock, exit 0.
    --live: chi khi co creds moi POST probe toi Workers AI
    (1 chat LLM + 1 embedding). Loi mang -> exit 1.

Vi du:
    python3 scripts/check_workers_ai.py
    python3 scripts/check_workers_ai.py --live
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maia.config import settings  # noqa: E402
from maia.embeddings_cloudflare import CloudflareEmbedder  # noqa: E402
from maia.llm import CloudflareLLM  # noqa: E402


def _mask(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return "(empty)"
    if len(v) <= 8:
        return v[:2] + "***"
    return v[:4] + "..." + v[-4:]


def report() -> tuple[CloudflareLLM, CloudflareEmbedder]:
    llm = CloudflareLLM(
        settings.CLOUDFLARE_ACCOUNT_ID,
        settings.CLOUDFLARE_API_TOKEN,
        settings.CLOUDFLARE_MODEL,
    )
    emb = CloudflareEmbedder(
        account_id=settings.CLOUDFLARE_ACCOUNT_ID,
        api_token=settings.CLOUDFLARE_API_TOKEN,
        model=settings.EMBED_MODEL,
    )
    print(f"account_id : {_mask(settings.CLOUDFLARE_ACCOUNT_ID)}")
    print("api_token  : (set)" if settings.CLOUDFLARE_API_TOKEN else "api_token  : (empty)")
    print(f"llm_model  : {settings.CLOUDFLARE_MODEL}")
    print(f"embed_model: {settings.EMBED_MODEL}")
    print(f"llm_mode   : {llm.mode}")
    print(f"embed_mode : {emb.mode}")
    if llm.mode == "mock":
        print("MOCK (no creds) — set CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN "
              "for real generation. Live probe skipped.")
    return llm, emb


def live_probe(llm: CloudflareLLM, emb: CloudflareEmbedder) -> int:
    if llm.mode != "cloudflare":
        print("MOCK (no creds) — live probe skipped, exit 0.")
        return 0
    try:
        ans = llm.chat(
            [{"role": "user", "content": "Say OK."}],
            max_tokens=16,
        )
        print(f"LLM probe OK: {ans[:120]!r}")
    except Exception as e:  # noqa: BLE001 — bao loi mang thanh exit code
        print(f"LLM probe FAILED: {type(e).__name__}: {e}")
        return 1
    try:
        vecs = emb.embed(["connectivity probe"])
        print(f"Embedding probe OK: shape={vecs.shape} dim={emb.dim}")
    except Exception as e:  # noqa: BLE001
        print(f"Embedding probe FAILED: {type(e).__name__}: {e}")
        return 1
    print("Workers AI connectivity: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MAIA Workers AI connectivity check")
    parser.add_argument("--live", action="store_true",
                        help="Thuc hien POST probe toi Workers AI (can creds that).")
    args = parser.parse_args()
    llm, emb = report()
    if args.live:
        return live_probe(llm, emb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

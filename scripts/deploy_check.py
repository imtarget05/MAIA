#!/usr/bin/env python3
"""Deployment verification script for MAIA.

Usage:
    python scripts/deploy_check.py https://maia-api.onrender.com

Checks:
    1. GET  /health              – service health + qdrant + llm_mode
    2. POST /auth/register       – create a throwaway test user
    3. POST /auth/login          – authenticate, grab bearer token
    4. POST /chat                – send a simple question (authenticated)
    5. POST /ingest/enterprise   – ingest enterprise docs (authenticated)

Exit code: 0 if all checks pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import UTC, datetime

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' is not installed. Run: pip install requests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results: list[tuple[str, bool, str]] = []  # (name, ok, detail)


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = PASS if ok else FAIL
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_health(base: str) -> str:
    """Return the access token if login succeeds, else ''."""
    print("\n[1/5] Health check  GET /health + GET /ready")
    try:
        r = requests.get(f"{base}/health", timeout=15)
        check("liveness HTTP 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code != 200:
            return ""
        live = r.json()
        check("liveness status == 'ok'", live.get("status") == "ok",
              f"got '{live.get('status')}'")
        r = requests.get(f"{base}/ready", timeout=60)
        check("readiness HTTP 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code != 200:
            return ""
        data = r.json()
    except Exception as exc:
        check("Response parseable", False, str(exc))
        return ""

    status_ok = data.get("status") == "ok"
    check("status == 'ok'", status_ok, f"got '{data.get('status')}'")

    qp = data.get("qdrant_points")
    check("qdrant_points > 0", isinstance(qp, int) and qp > 0, f"qdrant_points={qp}")

    llm_mode = data.get("llm_mode")
    check("llm_mode present", llm_mode is not None, f"llm_mode={llm_mode!r}")

    collection = data.get("collection", "?")
    embed_model = data.get("embed_model", "?")
    print(f"         collection={collection}  embed_model={embed_model}  llm_mode={llm_mode}")
    return data


def check_register(base: str) -> str | None:
    print("\n[2/5] Register      POST /auth/register")
    suffix = uuid.uuid4().hex[:8]
    email = f"deploy-check-{suffix}@example.com"
    payload = {
        "email": email,
        "password": "DeployCheck!234",
        "full_name": "Deploy Check",
        "department": "Engineering",
    }
    try:
        r = requests.post(f"{base}/auth/register", json=payload, timeout=15)
    except Exception as exc:
        check("Register request", False, str(exc))
        return None

    check("HTTP status 201", r.status_code == 201, f"got {r.status_code}: {r.text[:200]}")
    if r.status_code != 201:
        return None

    try:
        data = r.json()
    except Exception as exc:
        check("Response JSON", False, str(exc))
        return None

    user_id = data.get("id", "?")
    role = data.get("role", "?")
    print(f"         user_id={user_id}  role={role}  email={email}")
    return email


def check_login(base: str, email: str) -> str:
    print("\n[3/5] Login          POST /auth/login")
    payload = {"email": email, "password": "DeployCheck!234"}
    try:
        # NOTE: /auth/login uses OAuth2PasswordRequestForm -> form-encoded
        # username/password (NOT JSON). JSON body 422s with missing fields.
        r = requests.post(
            f"{base}/auth/login",
            data={"username": email, "password": "DeployCheck!234"},
            timeout=15,
        )
    except Exception as exc:
        check("Login request", False, str(exc))
        return ""

    check("HTTP status 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    if r.status_code != 200:
        return ""

    try:
        data = r.json()
    except Exception as exc:
        check("Response JSON", False, str(exc))
        return ""

    token = data.get("access_token", "")
    check("access_token present", bool(token), f"len={len(token)}")
    return token


def check_chat(base: str, token: str) -> None:
    print("\n[4/5] Chat           POST /chat")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"question": "What is the leave policy?", "top_k": 3}
    try:
        r = requests.post(f"{base}/chat", json=payload, headers=headers, timeout=30)
    except Exception as exc:
        check("Chat request", False, str(exc))
        return

    check("HTTP status 200", r.status_code == 200, f"got {r.status_code}: {r.text[:300]}")
    if r.status_code != 200:
        return

    try:
        data = r.json()
    except Exception as exc:
        check("Response JSON", False, str(exc))
        return

    status = data.get("status", "")
    has_evidence = data.get("has_evidence", False)
    answer_len = len(data.get("answer", ""))
    citations = data.get("citations", [])
    check("status is 'answered'", status == "answered", f"status={status!r}")
    check("has_evidence is True", has_evidence, f"has_evidence={has_evidence}")
    check("answer non-empty", answer_len > 0, f"len={answer_len}")
    check("citations present", len(citations) > 0, f"count={len(citations)}")


def check_ingest(base: str, token: str) -> None:
    print("\n[5/5] Enterprise     POST /ingest/enterprise")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.post(f"{base}/ingest/enterprise", headers=headers, timeout=60)
    except Exception as exc:
        check("Ingest request", False, str(exc))
        return

    check("HTTP status 200", r.status_code == 200, f"got {r.status_code}: {r.text[:300]}")
    if r.status_code != 200:
        return

    try:
        data = r.json()
    except Exception as exc:
        check("Response JSON", False, str(exc))
        return

    docs = data.get("documents_indexed", data.get("docs", data.get("documents", "?")))
    chunks = data.get("chunks_indexed", data.get("chunks", "?"))
    print(f"         documents_indexed={docs}  chunks_indexed={chunks}")
    check("Ingest returned success", True, f"docs={docs} chunks={chunks}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="MAIA deployment verification")
    parser.add_argument("base_url", help="Base URL of the deployed MAIA API (e.g. https://maia-api.onrender.com)")
    parser.add_argument("--timeout", type=int, default=90, help="Overall timeout in seconds (default 90)")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    print(f"\n{'='*60}")
    print("  MAIA Deployment Check")
    print(f"  Target : {base}")
    print(f"  Time   : {now_iso()}")
    print(f"{'='*60}")

    t0 = time.monotonic()

    check_health(base)
    email = check_register(base)
    token = check_login(base, email) if email else ""
    check_chat(base, token)
    check_ingest(base, token)

    elapsed = time.monotonic() - t0

    # Summary
    print(f"\n{'='*60}")
    print("  Summary")
    print(f"{'='*60}")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    for name, ok, detail in results:
        icon = "✓" if ok else "✗"
        print(f"  {icon} {name}" + (f"  ({detail})" if detail and not ok else ""))
    print(f"\n  Total: {total}  |  Passed: {passed}  |  Failed: {failed}  |  Elapsed: {elapsed:.1f}s")

    if failed:
        print(f"\n  \033[91mRESULT: FAIL — {failed} check(s) failed\033[0m\n")
        return 1
    print("\n  \033[92mRESULT: PASS — all checks passed\033[0m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

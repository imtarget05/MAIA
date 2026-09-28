"""Centralized LLM gateway resolution for every MAIA client path.

MAIA never talks to the LAN model host directly when the centralized
``llm-gateway`` proxy is available: all chat/completions and embeddings
traffic is sent to the OpenAI-compatible gateway at
``http://localhost:8787/v1`` with an ``X-Project: MAIA`` header so usage shows
up in the gateway's central telemetry (``llm-telemetry.jsonl``).

Resolution order for the OpenAI-compatible base URL (first entry wins, later
entries are fallbacks tried only when the previous host refuses the
connection):

1. the canonical env var -- ``LLM_BASE_URL`` / ``EMBEDDINGS_BASE_URL`` -- when
   it is explicitly set (``.env`` points it at the gateway);
2. ``LLM_GATEWAY_URL`` -- the centralized proxy;
3. ``LLM_DIRECT_UPSTREAM_URL`` -- the LAN LM Studio / Ollama host, used only
   when the gateway is not running, so a developer on the LAN with the
   gateway down still gets working LLM calls (degraded, never a hard
   failure).

Only *connection-level* failures (nothing listening on the port) fall through
to the next candidate. A read timeout or an HTTP error from a reachable
endpoint is never retried against another host: the request may already have
been executed upstream, and a second attempt would duplicate work.
"""
from __future__ import annotations

# Canonical project id the gateway attributes telemetry to. The gateway
# contract fixes the id set: MAIA, ApexInspect-AI.
PROJECT_ID = "MAIA"


def project_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers every outbound MAIA LLM/embedding request must carry."""
    headers = {"Content-Type": "application/json", "X-Project": PROJECT_ID}
    if extra:
        headers.update(extra)
    return headers


def _normalize(base_url: str) -> str:
    """Strip trailing slash and a full endpoint-path suffix."""
    base = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/embeddings"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def resolve_base_urls(*candidates: str | None) -> list[str]:
    """Ordered, de-duplicated list of base URLs to try (empty allowed).

    Accepts the raw values in priority order -- typically
    ``(explicit_env_value, gateway_url, direct_upstream_url)`` -- and returns
    the normalized, de-duplicated URLs. Values that are empty, blank, or
    ``"none"`` are dropped so an unset env var never becomes a candidate.
    """
    out: list[str] = []
    for raw in candidates:
        if raw is None:
            continue
        value = str(raw).strip()
        if not value or value.lower() in ("none", "null", "off", "disabled"):
            continue
        base = _normalize(value)
        if base and base not in out:
            out.append(base)
    return out


def split_timeout(timeout: float, connect_timeout: float) -> tuple[float, float]:
    """Build a ``requests`` ``(connect, read)`` timeout pair.

    The connect budget is deliberately short so that a gateway that is not
    running (or not reachable on the LAN) is skipped in a couple of seconds
    instead of blocking the caller for the full request timeout. Once a
    connection is established the read budget is the full configured timeout,
    so slow-but-working generations are unaffected.
    """
    read = float(timeout) if timeout else 120.0
    connect = float(connect_timeout) if connect_timeout else 3.0
    if connect <= 0 or connect > read:
        connect = read
    return (connect, read)

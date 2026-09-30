"""LLM via Cloudflare Workers AI (§9). MOCK fallback when no creds.

G-06: the external Cloudflare call is wrapped with a circuit breaker + retry.
On CircuitOpenError / timeout → graceful degraded (mock fallback answer),
never a hang.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import requests

from .config import settings
from .llm_endpoints import project_headers, resolve_base_urls, split_timeout

if TYPE_CHECKING:  # typing-only (runtime import would be circular via loops/)
    from .loops.resilience import CircuitBreaker, RetryConfig

# NOTE: loops.resilience is imported lazily inside _get_llm_breaker() to avoid
# a circular import at module load (loops/__init__ → corrective_rag → llm).

# G-06: per-dependency breaker + retry config for the LLM call.
_llm_breaker: CircuitBreaker | None = None
_llm_retry: RetryConfig | None = None


def _get_llm_breaker():
    global _llm_breaker, _llm_retry
    if _llm_breaker is None:
        from .loops.resilience import CircuitBreaker, RetryConfig
        threshold = settings.RELIABILITY_LLM_THRESHOLD or settings.RELIABILITY_FAILURE_THRESHOLD
        _llm_breaker = CircuitBreaker("llm", failure_threshold=threshold,
                                      recovery_timeout=settings.RELIABILITY_RECOVERY_TIMEOUT_SEC)
        _llm_retry = RetryConfig(max_retries=settings.RELIABILITY_MAX_RETRIES,
                                 backoff_base=settings.RELIABILITY_RETRY_BACKOFF_SEC,
                                 retryable=(TimeoutError, ConnectionError, OSError))
    return _llm_breaker


class LocalOpenAICompatLLM:
    """Local LLM via the OpenAI-compatible /v1/chat/completions endpoint.

    Requests go to the centralized llm-gateway first (with the
    ``X-Project: MAIA`` attribution header) and fall back to the direct LAN
    upstream only when the gateway refuses the connection.

    Same ``chat(messages) -> str`` surface as CloudflareLLM so agent/pipeline
    code works unchanged. ``mode`` is ``local`` (never ``mock``).
    Never raises: on any failure returns the mock text with a prefix.
    """

    def __init__(self, base_url: str = "", model: str = "", timeout: int = 120):
        # Gateway first, direct LAN upstream only as a connection-level
        # fallback (see maia.llm_endpoints). base_url is the explicit
        # override, when given it takes priority over the configured chain.
        self.base_urls = resolve_base_urls(
            base_url or settings.LLM_BASE_URL,
            settings.LLM_GATEWAY_URL,
            settings.LLM_DIRECT_UPSTREAM_URL,
        )
        self.base_url = self.base_urls[0] if self.base_urls else ""
        self.model = (model or settings.LLM_CHAT_MODEL).strip()
        self.timeout = int(timeout or settings.LLM_TIMEOUT_SEC)
        self.connect_timeout = float(settings.LLM_CONNECT_TIMEOUT_SEC)

    @property
    def mode(self) -> str:
        return "local"

    def _post_chat(self, base_url: str, messages: list[dict], max_tokens: int,
                   temperature: float) -> str:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers=project_headers(),
            json={"model": self.model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature, "stream": False},
            timeout=split_timeout(self.timeout, self.connect_timeout),
        )
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        return (choices[0].get("message", {}).get("content", "") if choices else "") or ""

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.1,
             top_p: float = 1.0, top_k: int = 50) -> str:
        last_error: Exception | None = None
        for base_url in self.base_urls:
            try:
                content = self._post_chat(base_url, messages, max_tokens, temperature)
            except requests.exceptions.ConnectionError as e:
                # Nothing listening (gateway down / host unreachable). Only
                # this is a safe fall-through: the request never reached a
                # model, so trying the next candidate cannot duplicate work.
                last_error = e
                continue
            except Exception as e:
                return f"[LLM local degraded: {e}] " + self._mock(messages)
            if content.strip():
                return content
            return self._mock(messages)
        if last_error is not None:
            return f"[LLM local degraded: {last_error}] " + self._mock(messages)
        return self._mock(messages)

    def _post_chat_stream(self, base_url: str, messages: list[dict], max_tokens: int,
                          temperature: float):
        """Yield raw text deltas via OpenAI-compatible SSE (stream:true).

        Yields nothing when upstream does not support SSE, so callers can
        fall back to sentence-chunked streaming honestly.
        """
        import json

        with requests.post(
            f"{base_url}/chat/completions",
            headers=project_headers(),
            json={"model": self.model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature, "stream": True},
            timeout=split_timeout(self.timeout, self.connect_timeout),
            stream=True,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                    delta = ((data.get("choices") or [{}])[0].get("delta") or {}).get("content", "")
                except Exception:
                    continue
                if delta:
                    yield delta

    def chat_stream(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.1):
        """Yield answer chunks for SSE (token-stream when upstream supports SSE,
        else honest sentence-chunked fallback of the completed answer)."""
        for base_url in self.base_urls:
            try:
                got_any = False
                for delta in self._post_chat_stream(base_url, messages, max_tokens, temperature):
                    got_any = True
                    yield delta
                if got_any:
                    return
                break
            except requests.exceptions.ConnectionError:
                continue
            except Exception:
                break
        ans = self.chat(messages, max_tokens=max_tokens, temperature=temperature)
        import re
        parts = re.split(r"(?<=[.!?])\s+", ans)
        for p in parts:
            if p:
                yield p + " "

    @staticmethod
    def _mock(messages: list[dict]) -> str:
        return CloudflareLLM._mock(messages)


def build_llm():
    """Pick the LLM backend from settings.LLM_PROVIDER.

    - ``local``   -> LocalOpenAICompatLLM (LAN LM Studio / gateway; default)
    - ``cloudflare`` -> CloudflareLLM (needs creds, else degrades to mock)
    - ``mock``    -> CloudflareLLM with no creds (deterministic offline text)

    Tests instantiate ``CloudflareLLM()`` directly and keep getting mock mode.
    """
    provider = (settings.LLM_PROVIDER or "").strip().lower()
    if provider in ("local", "lmstudio", "local_openai", "ollama"):
        return LocalOpenAICompatLLM(
            base_url=settings.LLM_BASE_URL,
            model=settings.LLM_CHAT_MODEL,
            timeout=settings.LLM_TIMEOUT_SEC,
        )
    return CloudflareLLM(settings.CLOUDFLARE_ACCOUNT_ID,
                         settings.CLOUDFLARE_API_TOKEN,
                         settings.CLOUDFLARE_MODEL)


class CloudflareLLM:
    def __init__(self, account_id: str = "", api_token: str = "", model: str = "@cf/meta/llama-3.1-8b-instruct"):
        self.account_id = (account_id or "").strip()
        self.api_token = (api_token or "").strip()
        self.model = model.strip() or "@cf/meta/llama-3.1-8b-instruct"

    @property
    def mode(self) -> str:
        return "cloudflare" if (self.account_id and self.api_token) else "mock"

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.1,
             top_p: float = 1.0, top_k: int = 50) -> str:
        if self.mode == "mock":
            return self._mock(messages)
        # G-06: lazy import to avoid circular import at module load.
        # Call _get_llm_breaker() FIRST so _llm_retry is initialized before use.
        from .loops.resilience import CircuitOpenError, RetryConfig, with_retry
        breaker = _get_llm_breaker()
        retry_cfg = _llm_retry if _llm_retry is not None else RetryConfig()
        try:
            # G-06: breaker + retry around the external call
            return with_retry(retry_cfg, breaker.call,
                              self._do_chat, messages, max_tokens, temperature, top_p, top_k)
        except (CircuitOpenError, TimeoutError, ConnectionError, OSError) as e:
            # G-06: graceful degraded → mock fallback instead of hanging/crashing
            return f"[LLM degraded: {e}] " + self._mock(messages)

    def _do_chat(self, messages, max_tokens, temperature, top_p, top_k) -> str:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/{self.model}"
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        r = requests.post(url, headers=headers,
                          json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                                "top_p": top_p, "top_k": top_k},
                          timeout=60)
        r.raise_for_status()
        data = r.json()
        # Workers AI returns {"result": {"response": "..."}} for instruct models
        res = data.get("result", {})
        if isinstance(res, dict):
            return res.get("response") or res.get("text") or str(res)
        return str(res)

    def chat_stream(self, messages: list[dict]):
        """Yield answer chunks for SSE."""
        ans = self.chat(messages)
        import re
        parts = re.split(r"(?<=[.!?])\s+", ans)
        for p in parts:
            if p:
                yield p + " "

    @staticmethod
    def _mock(messages: list[dict]) -> str:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # Extract context tags
        import re

        tags = re.findall(r"\[S\d+\]", user)
        uniq = []
        for t in tags:
            if t not in uniq:
                uniq.append(t)
        cite = " ".join(uniq[:5]) if uniq else "[S1]"
        # NOTE: no 'Sources:' footer — the UI renders structured citations
        # from the retrieval packet separately (clean ChatGPT-style display).
        return (
            f"(MOCK LLM - set CLOUDFLARE creds for real generation) Based on the retrieved context {cite}, "
            f"the answer is summarized from the top-ranked chunks. Please inspect Sources below for evidence."
        )

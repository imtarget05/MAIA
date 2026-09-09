"""LLM via Cloudflare Workers AI (§9). MOCK fallback when no creds.

G-06: the external Cloudflare call is wrapped with a circuit breaker + retry.
On CircuitOpenError / timeout → graceful degraded (mock fallback answer),
never a hang.
"""
import requests

from .config import settings

# NOTE: loops.resilience is imported lazily inside _get_llm_breaker() to avoid
# a circular import at module load (loops/__init__ → corrective_rag → llm).

# G-06: per-dependency breaker + retry config for the LLM call.
_llm_breaker = None
_llm_retry = None


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
        from .loops.resilience import CircuitOpenError, with_retry
        breaker = _get_llm_breaker()
        try:
            # G-06: breaker + retry around the external call
            return with_retry(_llm_retry, breaker.call,
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

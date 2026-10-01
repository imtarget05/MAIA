"""LLM via Cloudflare Workers AI (§9). MOCK fallback when no creds.

G-06: the external Cloudflare call is wrapped with a circuit breaker + retry.
On CircuitOpenError / timeout → graceful degraded (mock fallback answer),
never a hang.

Two invariants this module now holds, both of which used to be violated:

**Every fallback answer is identifiable.** A mock answer shaped like a real one
is a fabricated answer, and a caller that only checks "is the output non-empty"
cannot tell the two apart. `DEGRADED_MARKER` is the machine-detectable token
every degradation prefix carries, and every path that returns mock text
without having generated anything now carries one — including HTTP 200 with an
empty body, which used to return the bare mock text.

**Degradation is observable, not just visible in the prose.** `mode` is a STATIC
property of configuration (provider name / credential presence), never of what
a call did, so a probe reporting only `mode` cannot say generation is degraded.
`generation_state` (per instance) and `last_generation_state()` (per process)
report the OUTCOME of the last call. The process-level one is what `/ready`
reads: `build_stack()` builds a fresh adapter per request, so the object the
probe constructs has never been called.

Provider selection is CLOSED: `build_llm` maps every accepted name explicitly
and raises `LLMProviderConfigurationError` for anything else, instead of
letting an unknown value fall through to Cloudflare.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Final

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


# --------------------------------------------------------------------------- #
# Provider names: the closed set build_llm() accepts
# --------------------------------------------------------------------------- #
PROVIDER_LOCAL: Final[str] = "local"
PROVIDER_CLOUDFLARE: Final[str] = "cloudflare"
PROVIDER_MOCK: Final[str] = "mock"

#: Alternative spellings that all mean "local OpenAI-compatible endpoint".
#: Aliases, not a fuzzy match: `lm-studio` is NOT in here and must raise.
LOCAL_PROVIDER_ALIASES: Final[tuple[str, ...]] = (
    PROVIDER_LOCAL,
    "lmstudio",
    "local_openai",
    "ollama",
)

#: Every accepted value of `settings.LLM_PROVIDER`. A tuple so the error for a
#: typo lists the real options instead of a hand-typed copy that drifts.
SUPPORTED_PROVIDERS: Final[tuple[str, ...]] = (
    *LOCAL_PROVIDER_ALIASES,
    PROVIDER_CLOUDFLARE,
    PROVIDER_MOCK,
)


class LLMProviderConfigurationError(ValueError):
    """`settings.LLM_PROVIDER` is not a provider this build knows about.

    `ValueError` because it is a configuration value that is wrong, not a
    dependency that is missing — the same reasoning, and the same
    caller-compatibility guarantee, as
    `maia.retrieval_backends.BackendConfigurationError`.

    Deliberately NOT a `TypeError`. Nothing here would retry on it, but the
    distinction is kept for the same reason it is kept there: `except TypeError`
    around adapter calls is load-bearing in `retriever.retrieve`, and a
    configuration error must not be able to masquerade as one.
    """


# --------------------------------------------------------------------------- #
# Generation outcome: distinct from the STATIC `mode`
# --------------------------------------------------------------------------- #
#: The adapter has not been called yet in this process.
GENERATION_UNKNOWN: Final[str] = "unknown"
#: The last call returned text a real model produced.
GENERATION_OK: Final[str] = "ok"
#: The last call fell back to mock text because a dependency failed or is
#: misconfigured. The answer is NOT generated.
GENERATION_DEGRADED: Final[str] = "degraded"
#: The last call returned mock text because that is what was CONFIGURED
#: (`LLM_PROVIDER=mock`). Nothing failed — reporting this as `degraded` would
#: make a permanently-mock deployment look permanently broken.
GENERATION_MOCK: Final[str] = "mock"

#: The token every degraded prefix carries, so a caller (or a log scraper) can
#: detect fabrication without string-matching the human-readable message.
DEGRADED_MARKER: Final[str] = "degraded"

#: Process-wide outcome of the most recent LLM call. Module-level because
#: `build_stack()` constructs a NEW adapter per request: a per-instance flag
#: read off a freshly built object is always `unknown`, which would make the
#: readiness probe report "nothing has happened yet" forever instead of
#: "generation is degraded right now".
_last_generation_state: str = GENERATION_UNKNOWN
_last_generation_error: str = ""


def _record_generation(state: str, error: str = "") -> None:
    """Publish the outcome of one completed call, for `last_generation_state()`."""
    global _last_generation_state, _last_generation_error
    _last_generation_state = state
    _last_generation_error = error


def last_generation_state() -> tuple[str, str]:
    """`(state, error)` of the most recent LLM call in this process.

    `state` is one of the `GENERATION_*` constants. `error` is the short
    reason when `state` is `GENERATION_DEGRADED`, else `""`.
    """
    return _last_generation_state, _last_generation_error


class _GenerationOutcome:
    """Shared per-instance view of "what did MY last call return".

    A plain attribute rather than a property over a shared global, so a caller
    holding a specific adapter can see that adapter's outcome even when another
    request has since overwritten the process-wide value.
    """

    def _init_outcome(self) -> None:
        self.generation_state: str = GENERATION_UNKNOWN
        self._generation_error: str = ""

    def _record_outcome(self, state: str, error: str = "") -> None:
        self.generation_state = state
        self._generation_error = error
        _record_generation(state, error)

    @property
    def last_generation_error(self) -> str:
        return self._generation_error

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


class LocalOpenAICompatLLM(_GenerationOutcome):
    """Local LLM via the OpenAI-compatible /v1/chat/completions endpoint.

    Requests go to the centralized llm-gateway first (with the
    ``X-Project: MAIA`` attribution header) and fall back to the direct LAN
    upstream only when the gateway refuses the connection.

    Same ``chat(messages) -> str`` surface as CloudflareLLM so agent/pipeline
    code works unchanged. ``mode`` is ``local`` (never ``mock``) — it is the
    STATIC configured provider; ``generation_state`` is what the last call
    actually did.

    Never raises: on any failure returns the mock text with a prefix. Every
    such prefix carries :data:`DEGRADED_MARKER`, so a fabricated answer is
    machine-detectable, not just readable.
    """

    #: The provider name this adapter implements, for callers that compare
    #: objects rather than classes (see `build_llm`).
    provider: str = PROVIDER_LOCAL

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
        self._init_outcome()

    @property
    def mode(self) -> str:
        return PROVIDER_LOCAL
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

    def _degraded(self, messages: list[dict], reason: object) -> str:
        """Mock text behind an explicit, machine-detectable marker.

        Every fabricated answer goes through here. The prefix is the human
        contract that already existed; routing all paths through one function
        is what makes "no path returns bare mock text" checkable instead of a
        comment.
        """
        self._record_outcome(GENERATION_DEGRADED, str(reason))
        return f"[LLM local degraded: {reason}] " + self._mock(messages)

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
                return self._degraded(messages, e)
            if content.strip():
                self._record_outcome(GENERATION_OK)
                return content
            # HTTP 200 with no content. Previously the bare mock text, which is
            # indistinguishable from a deliberate mock run and from a real
            # answer as far as a non-empty check is concerned. A reachable
            # endpoint that cannot answer is a DEGRADATION, so it says so.
            return self._degraded(messages, f"empty response from {base_url}")
        if last_error is not None:
            return self._degraded(messages, last_error)
        # No endpoint at all: the for-loop never ran. Also fabricated text.
        return self._degraded(messages, "no LLM endpoint configured")

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
            for raw in r.iter_lines(decode_unicode=True):
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
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
    """Pick the LLM backend from settings.LLM_PROVIDER. CLOSED mapping.

    - ``local`` / ``lmstudio`` / ``local_openai`` / ``ollama``
      -> ``LocalOpenAICompatLLM`` (LAN LM Studio via the gateway; the default)
    - ``cloudflare`` -> ``CloudflareLLM`` with the configured creds
    - ``mock`` -> ``CloudflareLLM`` FORCED to no creds: deterministic offline
      text, and it can never reach the network even when creds are present

    Anything else raises. The previous implementation had one explicit branch
    and a `return` for everything else, so `LLM_PROVIDER=clodflare` silently
    became Cloudflare-with-credentials and `LLM_PROVIDER=mock` produced an
    object indistinguishable from `LLM_PROVIDER=cloudflare`. A typo in a deploy
    config is invisible that way; here it is a boot-time failure that names the
    accepted values.

    Tests instantiate ``CloudflareLLM()`` directly and keep getting mock mode
    when they pass no creds.

    Raises:
        LLMProviderConfigurationError: ``LLM_PROVIDER`` is not in
            ``SUPPORTED_PROVIDERS``.
    """
    provider = (settings.LLM_PROVIDER or "").strip().lower()
    if provider in LOCAL_PROVIDER_ALIASES:
        return LocalOpenAICompatLLM(
            base_url=settings.LLM_BASE_URL,
            model=settings.LLM_CHAT_MODEL,
            timeout=settings.LLM_TIMEOUT_SEC,
        )
    if provider == PROVIDER_CLOUDFLARE:
        return CloudflareLLM(settings.CLOUDFLARE_ACCOUNT_ID,
                             settings.CLOUDFLARE_API_TOKEN,
                             settings.CLOUDFLARE_MODEL,
                             provider=PROVIDER_CLOUDFLARE)
    if provider == PROVIDER_MOCK:
        # Empty creds are load-bearing, not cosmetic: `mode` and therefore the
        # no-creds branch in `chat` both key off them, so passing the ambient
        # credentials here is what made `mock` and `cloudflare` the same object.
        return CloudflareLLM("", "", settings.CLOUDFLARE_MODEL, provider=PROVIDER_MOCK)
    raise LLMProviderConfigurationError(
        f"Unknown LLM_PROVIDER {provider!r}. "
        f"Supported: {', '.join(SUPPORTED_PROVIDERS)}."
    )


class CloudflareLLM(_GenerationOutcome):
    def __init__(self, account_id: str = "", api_token: str = "",
                 model: str = "@cf/meta/llama-3.1-8b-instruct",
                 provider: str = ""):
        self.account_id = (account_id or "").strip()
        self.api_token = (api_token or "").strip()
        self.model = model.strip() or "@cf/meta/llama-3.1-8b-instruct"
        # The CONFIGURED provider, kept separately from `mode` so a deliberate
        # `mock` is distinguishable from a `cloudflare` that has no creds: both
        # read `mode == "mock"`, only one is what anybody asked for. Left empty
        # for direct construction (tests), where the creds are then the only
        # evidence of intent available.
        self.provider = (provider or "").strip().lower()
        self._init_outcome()

    @property
    def mode(self) -> str:
        """The STATIC configuration: which transport this object would use.

        Never the outcome of a call — see `generation_state` for that.
        """
        return PROVIDER_CLOUDFLARE if (self.account_id and self.api_token) else PROVIDER_MOCK

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.1,
             top_p: float = 1.0, top_k: int = 50) -> str:
        if self.mode == PROVIDER_MOCK:
            if self.provider == PROVIDER_MOCK:
                # Asked for. Nothing failed, so this is not a degradation.
                self._record_outcome(GENERATION_MOCK)
            else:
                # No creds on a backend that was NOT configured as mock: a
                # misconfiguration, and the answer is fabricated either way.
                self._record_outcome(GENERATION_DEGRADED, "missing CLOUDFLARE credentials")
            return self._mock(messages)
        # G-06: lazy import to avoid circular import at module load.
        # Call _get_llm_breaker() FIRST so _llm_retry is initialized before use.
        from .loops.resilience import CircuitOpenError, RetryConfig, with_retry
        breaker = _get_llm_breaker()
        retry_cfg = _llm_retry if _llm_retry is not None else RetryConfig()
        try:
            # G-06: breaker + retry around the external call
            answer = with_retry(retry_cfg, breaker.call,
                                self._do_chat, messages, max_tokens, temperature, top_p, top_k)
        except (CircuitOpenError, TimeoutError, ConnectionError, OSError) as e:
            # G-06: graceful degraded → mock fallback instead of hanging/crashing
            self._record_outcome(GENERATION_DEGRADED, str(e))
            return f"[LLM degraded: {e}] " + self._mock(messages)
        self._record_outcome(GENERATION_OK)
        return answer
    def _do_chat(self, messages, max_tokens, temperature, top_p, top_k) -> str:
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/{self.model}"
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        r = requests.post(url, headers=headers,
                          json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                                "top_p": top_p, "top_k": top_k},
                          timeout=60)
        r.raise_for_status()
        data = r.json()
        res = data.get("result", {})
        if isinstance(res, dict):
            choices = res.get("choices")
            if isinstance(choices, list) and choices:
                content = choices[0].get("message", {}).get("content")
                if content is not None:
                    return content
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

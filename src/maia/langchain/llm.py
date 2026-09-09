"""LangChain adapter for MAIA's Cloudflare LLM (§9).

This is the *LangChain* role in MAIA's stack: **abstraction layer** over the
existing ``CloudflareLLM`` so it can sit inside a LangChain / LangGraph pipeline
without discarding guardrails (circuit-breaker, retry, mock fallback already
built into the underlying adapter; boundary defense lives in ``prompts/``).

We implement ``BaseChatModel`` so nodes get:
  * invoke / ainvoke       -> synchronous + async calls
  * _stream                -> yields ``AIMessageChunk`` tokens for SSE
  * bind_tools             -> optional tool-calling enhancement
"""
from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from ..llm import CloudflareLLM

__all__ = ["CloudflareLangChainAdapter", "bind_maia_tools", "cloudflare_lcel"]


def _to_chat_messages(messages: list[BaseMessage]) -> list[dict]:
    """Convert LangChain message list -> CloudflareLLM dict list."""
    _ROLE = {"system": "system", "human": "user", "ai": "assistant",
             "assistant": "assistant", "tool": "user"}
    out: list[dict] = []
    for m in messages:
        role = getattr(m, "type", "human")
        out.append({"role": _ROLE.get(role, role), "content": str(m.content)})
    return out


class CloudflareLangChainAdapter(BaseChatModel):
    """LangChain ``BaseChatModel`` wrapping MAIA's ``CloudflareLLM``.

    Keeps a reference to the underlying adapter (which already implements the
    G-06 circuit-breaker + retry + mock fallback contract).
    """

    model_name: str = "maia-cloudflare-llm"

    def __init__(self, underlying: CloudflareLLM, max_tokens: int = 512,
                 temperature: float = 0.1, top_p: float = 1.0, top_k: int = 50,
                 **kwargs: Any):
        super().__init__(**kwargs)
        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(self, "_max_tokens", max_tokens)
        object.__setattr__(self, "_temperature", temperature)
        object.__setattr__(self, "_top_p", top_p)
        object.__setattr__(self, "_top_k", top_k)

    @property
    def _llm_type(self) -> str:
        return "maia-cloudflare-llm"

    @property
    def mode(self) -> str:
        """Exposed so downstream grounding/evidence gates match CloudflareLLM."""
        return self.underlying.mode

    @property
    def _default_params(self) -> dict:
        return {"max_tokens": self._max_tokens, "temperature": self._temperature,
                "top_p": self._top_p, "top_k": self._top_k}

    def _generate(self, messages, stop=None,
                  run_manager: CallbackManagerForLLMRun | None = None,
                  **kwargs: Any) -> ChatResult:
        lc_msgs = _to_chat_messages(messages)
        params = {**self._default_params, **kwargs}
        text = self.underlying.chat(
            lc_msgs,
            max_tokens=params.get("max_tokens", self._max_tokens),
            temperature=params.get("temperature", self._temperature),
            top_p=params.get("top_p", self._top_p),
            top_k=params.get("top_k", self._top_k),
        )
        gen = ChatGeneration(
            message=AIMessage(content=text),
            generation_info={"model": self.underlying.model,
                             "mode": self.underlying.mode},
        )
        return ChatResult(generations=[gen])

    def _stream(self, messages, *, stop=None,
                run_manager: CallbackManagerForLLMRun | None = None,
                **kwargs: Any):
        """Yield ``ChatGenerationChunk`` tokens reusing ``CloudflareLLM.chat_stream``.

        langchain-core 1.6's base ``stream`` unwraps each
        ``ChatGenerationChunk.message`` (an ``AIMessageChunk``), so we must emit
        ``ChatGenerationChunk`` here rather than bare ``AIMessageChunk``.
        """
        lc_msgs = _to_chat_messages(messages)
        for chunk in self.underlying.chat_stream(lc_msgs):
            if chunk:
                yield ChatGenerationChunk(message=AIMessageChunk(content=chunk))

    async def _astream(self, messages, *, stop=None,
                       run_manager: CallbackManagerForLLMRun | None = None,
                       **kwargs: Any):
        """Async streaming that reuses the synchronous ``chat_stream`` via executor."""
        import asyncio

        loop = asyncio.get_event_loop()
        gen = self._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
        try:
            while True:
                chunk = await loop.run_in_executor(None, next, gen)
                yield chunk
        except StopIteration:
            pass


def cloudflare_lcel(model_name: str | None = None) -> CloudflareLangChainAdapter:
    """Build a ready-to-invoke LangChain adapter wired to ``settings`` credentials.

    Convenience entry for LangGraph nodes / builders: returns a
    ``BaseChatModel`` that is already ``Messages -> AIMessage`` callable.
    """
    from ..config import settings

    underlying = CloudflareLLM(
        account_id=settings.CLOUDFLARE_ACCOUNT_ID,
        api_token=settings.CLOUDFLARE_API_TOKEN,
        model=model_name or settings.CLOUDFLARE_MODEL,
    )
    return CloudflareLangChainAdapter(underlying)


def bind_maia_tools(llm: BaseChatModel):
    """Bind MAIA's LangChain tools onto *llm* for optional native tool-calling.

    Enhancement only: on mock / non-tool-capable endpoints the binding is
    silently ignored -- the agent layer keeps explicit tool dispatch as the
    source of truth.
    """
    try:
        from ..agent.tools import TOOL_REGISTRY_LC
        return llm.bind_tools(list(TOOL_REGISTRY_LC.values()), tool_choice=None)
    except Exception:
        return llm

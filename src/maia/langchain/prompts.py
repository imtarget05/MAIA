"""LangChain prompt templates for MAIA.

Wraps the *existing* prompt strings defined in ``maia.prompt`` (``SYSTEM_PROMPT``,
``AGENT_SYSTEM_PROMPT``, the ``<retrieved_document>`` boundary rule) into
``ChatPromptTemplate`` objects so LangGraph nodes can use the LCEL
``prompt | llm`` idiom.

The defense text is shared verbatim — only the container changes (raw string ->
ChatPromptTemplate).  No prompt *content* is altered, keeping the prompt-
injection boundary defense (P1-7) intact.
"""
from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from ..prompt import (
    AGENT_SYSTEM_PROMPT,
    BOUNDARY_RULE,
    SYSTEM_PROMPT,
)

__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "BOUNDARY_RULE",
    "SYSTEM_PROMPT",
    "agent_prompt",
    "rag_prompt",
    "structured_classify_prompt",
]

# ---------------------------------------------------------------------------
# RAG (single-turn) prompt — used by simple-answer / knowledge-retrieval nodes
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape literal ``{`` / ``}`` so ChatPromptTemplate treats them as text,
    not replacement fields.  (The existing prompts contain JSON examples and
    boundary-tag examples with literal braces.)"""
    return text.replace("{", "{{").replace("}", "}}")

rag_prompt = ChatPromptTemplate.from_messages([
    ("system", _esc(SYSTEM_PROMPT)),
    ("human", "Context:\n{context}\n\nQuestion: {question}\nAnswer with citations [S1], [S2]..."),
])

# ---------------------------------------------------------------------------
# Agent prompt (multi-turn) — used by the generate node with history
# ---------------------------------------------------------------------------

agent_prompt = ChatPromptTemplate.from_messages([
    ("system", _esc(AGENT_SYSTEM_PROMPT)),
    MessagesPlaceholder("history", optional=True),
    ("human", "Context:\n{context}\n\nQuestion: {question}\nAnswer with citations [S1], [S2]..."),
])

# ---------------------------------------------------------------------------
# Structured classification prompt — intent + action decision
# ---------------------------------------------------------------------------

CLASSIFY_INSTRUCTION = (
    "You are MAIA's query router. Classify the user question into EXACTLY one "
    "of these intents: leave_request, leave_balance, it_help, vpn, expense, "
    "benefits, hr_policy, onboarding, security, general.\n"
    "Then decide whether the user wants an action performed. Output ONLY JSON:\n"
    '{"intent": "<intent>", "action": "<check_leave_balance|'
    'create_leave_request|create_it_ticket|get_employee_requests|none>", "slots": {}}.'
)

structured_classify_prompt = ChatPromptTemplate.from_messages([
    ("system", _esc(CLASSIFY_INSTRUCTION)),
    ("human", "{question}"),
])


def build_prompt(question: str, context: str) -> list[dict]:
    """Thin LCEL-free helper mirroring ``prompt.build_messages`` for the legacy
    CloudflareLLM path — kept for nodes that bypass the LangChain LLM adapter.
    """
    # Imported here to avoid a circular import at module load time.
    from ..prompt import build_messages
    return build_messages(question, context)

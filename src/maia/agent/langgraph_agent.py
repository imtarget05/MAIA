"""LangGraph agent — the *control plane* of MAIA's AI stack.

While ``langchain/`` is the abstraction layer (LLM / tools / prompts) and the
retriever is the *data plane*, THIS module is the *orchestrator*: a typed,
stateful, streaming-capable LangGraph ``StateGraph`` that ties the pieces
together into the agentic RAG workflow.

Architecture:
                 START
                   │
            ┌──────▼──────┐
            │ classify_   │  intent + action decision
            │ query       │
            └──┬───┬───┬──┘
               │   │   │
     trivial/  │   │   │  knowledge /
     greeting  │   │   │  action-needed
               ▼   │   ▼
           simple_ │  retrieve ─► rerank ─► generate ─► verify_grounding
           answer  │                                   │          │
               │   │                                 pass       fail
               │   │                                  │          │
               │   └──────────── retry ◄───────────────┘     retry_retrieve
               │                                      (max retries → END)
               └─────────────────────► END

State is a Pydantic model (validated, serializable) so the graph can be
checkpointed (durable execution) and streamed node-by-node.
"""
from __future__ import annotations

from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from ..config import settings
from .intent_router import IntentRouter
from .intents import detect_intent
from .schemas import INSUFFICIENT_TEXT
from .session import session_store

__all__ = [
    "DEFAULT_TOOLS",
    "AgentState",
    "build_graph",
    "get_durable_graph",
    "graph",
    "resume_from_approval",
]


class AgentState(BaseModel):
    """Typed, validated state threaded through every LangGraph node.

    Pydantic gives us validation + serialization for free (needed for
    checkpointing / durable execution).  Fields default to empty values so a
    node can return only the keys it updates (LangGraph merges partial updates).
    """

    question: str = ""
    session_id: str = "default"
    tenant_id: str = "default"
    employee_id: str | None = None
    requester_email: str | None = None

    intent: str = "general"
    slots: dict = Field(default_factory=dict)
    plan: dict = Field(default_factory=dict)

    candidates: list = Field(default_factory=list)
    context: str = ""
    used_chunks: list = Field(default_factory=list)
    retries: int = 0

    answer: str = ""
    citations: list = Field(default_factory=list)
    has_evidence: bool = False
    grounding_score: float = 0.0
    cites_valid: bool = True

    pending_action: dict | None = None
    action_result: dict | None = None
    approval_needed: bool = False

    status: str = "pending"
    flags: list = Field(default_factory=list)
    llm_mode: str = "mock"
    rerank_mode: str = "fallback"
    node_trace: list = Field(default_factory=list)

    def trace(self, node: str) -> None:
        self.node_trace.append(node)


def _build_stack(tenant_id: str | None = None):
    """Build the existing retrieval/LLM stack (reuses pipeline_query.build_stack)."""
    from ..pipeline_query import build_stack
    return build_stack(tenant_id=tenant_id)


def node_classify_query(state: AgentState) -> AgentState:
    """Decide intent + action plan from the raw question.

    Extracts slots first so the IntentRouter can decide whether all required
    slots are present (e.g. leave_request needs both ``days`` and
    ``start_date``) — without slots the router would treat every action intent
    as "needs clarification" and never propose a tool.
    """
    state.trace("classify_query")
    from .intents import slots_for_intent
    intent = detect_intent(state.question)
    slots = slots_for_intent(state.question, intent)
    plan = IntentRouter().decide(intent, state.question, slots)
    state.intent = intent
    state.slots = slots
    state.plan = plan
    return state


def node_simple_answer(state: AgentState) -> AgentState:
    """Handle trivial/greeting questions without retrieval."""
    state.trace("simple_answer")
    state.answer = (
        "Xin chào! Tôi là MAIA, trợ lý nhân viên. Bạn có thể hỏi tôi về chính sách "
        "nhân sự, IT, nghỉ phép, hoặc yêu cầu tạo ticket/nghỉ phép."
    )
    state.status = "answered"
    state.has_evidence = False
    return state


def node_retrieve(state: AgentState) -> AgentState:
    """Hybrid retrieval (dense + BM25 -> RRF) scoped to tenant/session.

    Default: the existing ``HybridRetriever`` (dense Qdrant + BM25, RRF-fused).
    Opt-in: when ``settings.LLAMA_INDEX_DATA_PLANE`` is True, the DENSE path
    runs through a LlamaIndex ``VectorStoreIndex`` (backed by the existing
    QdrantStore via ``MaiaQdrantStore`` — same collection, no new packages)
    while the SPARSE path still uses the existing ``HybridRetriever``'s BM25
    index; both lists are RRF-fused (k=60, see ``_llama_index_retrieve``).
    """
    state.trace("retrieve")
    if settings.LLAMA_INDEX_DATA_PLANE:
        state.candidates = _llama_index_retrieve(state)
        return state
    _, _, retriever, _, _ = _build_stack(state.tenant_id)
    state.candidates = retriever.retrieve(
        state.question, tenant_id=state.tenant_id, session_id=state.session_id)
    return state


def _llama_index_retrieve(state: AgentState) -> list[dict]:
    """Hybrid retrieval: LlamaIndex dense + BM25 → RRF fusion.

    Dense path: LlamaIndex ``VectorStoreIndex`` over the existing Qdrant store
    (via ``MaiaQdrantStore``).  Sparse path: the existing ``HybridRetriever``'s
    BM25 index (tenant-scoped, JSON corpus cache).  The two result lists are
    fused with Reciprocal Rank Fusion (RRF, k=60) — the same algorithm the
    default ``HybridRetriever.retrieve`` uses, so scores are comparable.
    """
    embedder, store, hybrid_retriever, _, _ = _build_stack(state.tenant_id)
    from llama_index.core import Settings, VectorStoreIndex
    from llama_index.core.embeddings import BaseEmbedding

    from maia.llamaindex_store import MaiaQdrantStore

    class _MaiaEmbed(BaseEmbedding):
        """Bridge MAIA's Embedder into LlamaIndex's BaseEmbedding interface."""
        def _get_text_embedding(self, text):
            return embedder.embed([text]).tolist()[0]
        def _get_query_embedding(self, text):
            return embedder.embed_query(text).tolist()
        def _get_text_embeddings(self, texts):
            return embedder.embed(texts).tolist()
        async def _aget_text_embedding(self, text):
            return self._get_text_embedding(text)
        async def _aget_query_embedding(self, text):
            return self._get_query_embedding(text)
        async def _aget_text_embeddings(self, texts):
            return self._get_text_embeddings(texts)

    # ---- Dense: LlamaIndex VectorStoreIndex ----
    Settings.embed_model = _MaiaEmbed()
    vs = MaiaQdrantStore(store, tenant_id=state.tenant_id)
    index = VectorStoreIndex.from_vector_store(vs)
    li_retriever = index.as_retriever(similarity_top_k=settings.TOP_K_DENSE)
    dense_hits: list[dict] = []
    try:
        hits = li_retriever.retrieve(state.question)
        for h in hits:
            node = getattr(h, "node", None)
            if node is None:
                continue
            meta = dict(getattr(node, "metadata", {}) or {})
            dense_hits.append({
                "chunk_id": getattr(node, "id_", "") or meta.get("chunk_id", ""),
                "text": node.get_content(),
                "score": float(getattr(h, "score", 0.0) or 0.0),
                "metadata": meta,
            })
    except Exception:
        dense_hits = []

    # ---- Sparse: BM25 from the existing HybridRetriever ----
    bm25_hits: list[dict] = []
    bm25 = getattr(hybrid_retriever, "_bm25", None)
    corpus = getattr(hybrid_retriever, "_corpus", None) or []
    if bm25 is not None and corpus:
        try:
            import numpy as np

            from maia.textnorm import norm_tokens
            scores = bm25.get_scores(norm_tokens(state.question))
            top_n = getattr(hybrid_retriever, "top_k_bm25", 10)
            idx = np.argsort(scores)[::-1][:top_n]
            for i in idx:
                if scores[i] <= 0:
                    continue
                c = corpus[int(i)]
                meta = c.get("metadata", {}) or {}
                tid = meta.get("tenant_id")
                if state.tenant_id and tid not in (state.tenant_id, None, ""):
                    continue
                bm25_hits.append({
                    "chunk_id": c["chunk_id"],
                    "text": c["text"],
                    "score": float(scores[i]),
                    "metadata": meta,
                })
        except Exception:
            bm25_hits = []

    # ---- RRF fusion (k=60, same as HybridRetriever default) ----
    rrf_k = getattr(hybrid_retriever, "rrf_k", 60)
    fused: dict[str, dict] = {}
    for rank, d in enumerate(dense_hits):
        cid = d["chunk_id"]
        e = fused.setdefault(cid, {"chunk_id": cid, "text": d["text"],
                                   "metadata": d["metadata"],
                                   "dense_score": 0.0, "bm25_score": 0.0,
                                   "fused_score": 0.0})
        e["dense_score"] = d["score"]
        e["fused_score"] += 1.0 / (rrf_k + rank + 1)
    for rank, b in enumerate(bm25_hits):
        cid = b["chunk_id"]
        e = fused.setdefault(cid, {"chunk_id": cid, "text": b["text"],
                                   "metadata": b["metadata"],
                                   "dense_score": 0.0, "bm25_score": 0.0,
                                   "fused_score": 0.0})
        e["bm25_score"] = b["score"]
        e["fused_score"] += 1.0 / (rrf_k + rank + 1)

    ranked = sorted(fused.values(), key=lambda x: x["fused_score"], reverse=True)
    top_k_fused = getattr(hybrid_retriever, "top_k_fused", settings.TOP_K_FINAL)
    return ranked[:top_k_fused]


def node_rerank(state: AgentState) -> AgentState:
    """Cross-encoder rerank (falls back to RRF order)."""
    state.trace("rerank")
    _, _, _, reranker, _ = _build_stack(state.tenant_id)
    reranked = reranker.rerank(state.question, state.candidates,
                               top_k=settings.TOP_K_FINAL)
    state.rerank_mode = reranker.mode
    from ..prompt import assemble
    context, used = assemble(reranked)
    state.context = context
    state.used_chunks = used
    return state


def node_generate(state: AgentState) -> AgentState:
    """Generate a grounded, cited answer from the assembled context."""
    state.trace("generate")
    _, _, _, _, llm = _build_stack(state.tenant_id)
    state.llm_mode = llm.mode

    if not state.used_chunks:
        state.answer = INSUFFICIENT_TEXT
        state.has_evidence = False
        state.status = "refused"
        return state

    from ..prompt import build_agent_messages
    history_text = session_store.history_text(state.session_id,
                                              tenant_id=state.tenant_id)
    messages = build_agent_messages(state.question, state.context, history_text)
    answer = llm.chat(messages)
    try:
        from ..answer_format import clean_answer
        answer = clean_answer(answer)
    except Exception:
        pass
    state.answer = answer
    return state


def node_verify_grounding(state: AgentState) -> AgentState:
    """Grounding + citation check (Loop 3)."""
    state.trace("verify_grounding")
    from ..loops.answer_loop import CitationChecker, GroundingChecker
    grounding = GroundingChecker(threshold=settings.AGENT_GROUNDING_THRESHOLD)
    cc = CitationChecker(state.used_chunks)
    grounded, score = grounding.check(state.answer, state.context)
    cites_valid, _ = cc.check(state.answer)
    state.grounding_score = round(score, 4)
    state.cites_valid = cites_valid
    state.has_evidence = grounded and cites_valid
    # Populate citations (contract: chunks the answer actually cites).
    # Legacy pipeline returns all used chunks; here we keep only the ranks
    # the answer references ([S1]..[Sn]) so the API/UI never render [].
    try:
        from ..loops.answer_loop import extract_cites
        if state.has_evidence:
            ranks = set(extract_cites(state.answer))
            state.citations = [
                c for i, c in enumerate(state.used_chunks, start=1) if i in ranks
            ]
        else:
            state.citations = []
    except Exception:
        state.citations = []
    return state


def node_retry_retrieve(state: AgentState) -> AgentState:
    """Bump the retry counter; the conditional edge decides whether to loop."""
    state.trace("retry_retrieve")
    state.retries += 1
    return state


def node_propose_action(state: AgentState) -> AgentState:
    """Propose a side-effecting action and PAUSE for human approval (HITL).

    On first run: builds the proposal and calls ``interrupt(proposal)`` — the
    graph halts.  The proposal is carried in the ``interrupt`` VALUE (state
    mutations made *before* an interrupt are NOT committed to the checkpoint),
    so the API reads it from ``__interrupt__[0].value`` and renders the
    approval card.

    On resume (``Command(resume=<decision>)``): the node re-runs, rebuilds the
    proposal (idempotent), ``interrupt()`` returns the approval decision, and
    we execute or cancel.  Mutations below the interrupt ARE committed.
    """
    state.trace("propose_action")
    tool = (state.plan or {}).get("tool")
    if not tool:
        state.status = "answered"
        return state

    # Idempotent proposal (rebuilt on every entry, including resume).
    proposal = {
        "tool": tool,
        "params": dict(state.slots),
        "employee_id": state.employee_id,
        "tenant_id": state.tenant_id,
        "summary": f"Đề xuất {tool} (chưa thực hiện — cần xác nhận)",
    }

    # HITL: pause here. The proposal is communicated to the client via the
    # interrupt value.  On resume, interrupt() returns the approval decision.
    decision = interrupt(proposal)

    # --- resume path only (decision is the approval payload) ---
    if _is_approved(decision):
        result = _execute_tool(tool, proposal["params"], state.employee_id, state.tenant_id)
        state.action_result = {"type": tool, "result": result}
        state.status = "action_completed" if (result or {}).get("ok") else "action_failed"
    else:
        state.status = "action_cancelled"
    state.pending_action = proposal
    state.approval_needed = False
    return state


def _is_approved(decision) -> bool:
    """Normalize a resume value (bool or dict) into an approval flag."""
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, dict):
        return bool(decision.get("approved", False))
    return bool(decision)


def _execute_tool(tool: str, params: dict, employee_id: str | None, tenant_id: str | None) -> dict:
    """Execute a side-effect tool from ``TOOL_REGISTRY`` with tenant-aware params."""
    from .tools import TOOL_REGISTRY
    fn = TOOL_REGISTRY.get(tool)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {tool}"}
    kw = dict(params)
    if employee_id:
        kw.setdefault("employee_id", employee_id)
    if tenant_id:
        kw.setdefault("tenant_id", tenant_id)
    try:
        return fn(**kw)
    except TypeError as e:
        return {"ok": False, "error": f"Tool arg error: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def node_finalize(state: AgentState) -> AgentState:
    """Mark the run as answered (terminal helper for readable edges)."""
    state.trace("finalize")
    if state.status in ("pending",):
        state.status = "answered"
    return state


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------


def route_after_classify(state: AgentState) -> Literal["simple_answer", "retrieve"]:
    """Route after intent classification.

    Decision: ``general`` routes to ``retrieve`` (NOT ``simple_answer``).
    ``general`` is the catch-all for knowledge questions that did not match a
    specific keyword pattern — skipping retrieval for those would mean the LLM
    answers without any grounded context, which defeats the RAG contract.
    Only truly trivial chit-chat (currently none — no ``greeting`` intent exists
    in the taxonomy) would skip retrieval.
    """
    if state.intent == "greeting":
        return "simple_answer"
    return "retrieve"


def route_after_verify(state: AgentState) -> Literal["propose_action", "finalize", "retry_retrieve"]:
    """On good grounding -> finalize (or propose action if one is planned).
    On bad grounding -> retry while budget remains."""
    if state.has_evidence:
        if (state.plan or {}).get("tool") and state.intent not in ("leave_balance",):
            return "propose_action"
        return "finalize"
    if state.retries < max(1, settings.AGENT_MAX_ITER):
        return "retry_retrieve"
    return "finalize"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(*, checkpointer=None):
    """Compile the full LangGraph agent.

    ``checkpointer`` enables durable execution (state persisted across nodes /
    restarts).  Pass a ``MemorySaver``/``SqliteSaver`` for checkpointing, or
    ``None`` for in-memory only.
    """
    builder = StateGraph(AgentState)

    builder.add_node("classify_query", node_classify_query)
    builder.add_node("simple_answer", node_simple_answer)
    builder.add_node("retrieve", node_retrieve)
    builder.add_node("rerank", node_rerank)
    builder.add_node("generate", node_generate)
    builder.add_node("verify_grounding", node_verify_grounding)
    builder.add_node("retry_retrieve", node_retry_retrieve)
    builder.add_node("propose_action", node_propose_action)
    builder.add_node("finalize", node_finalize)

    builder.add_edge(START, "classify_query")
    builder.add_conditional_edges(
        "classify_query",
        route_after_classify,
        {"simple_answer": "simple_answer", "retrieve": "retrieve"},
    )
    builder.add_edge("simple_answer", END)
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "generate")
    builder.add_edge("generate", "verify_grounding")
    builder.add_conditional_edges(
        "verify_grounding",
        route_after_verify,
        {"propose_action": "propose_action", "finalize": "finalize",
         "retry_retrieve": "retry_retrieve"},
    )
    builder.add_edge("retry_retrieve", "retrieve")
    builder.add_edge("propose_action", END)
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Module-level convenience: compiled in-memory graph (with MemorySaver).
# ---------------------------------------------------------------------------

graph = build_graph(checkpointer=MemorySaver())

DEFAULT_TOOLS = ["check_leave_balance", "create_leave_request",
                  "create_it_ticket", "get_employee_requests"]


# ---------------------------------------------------------------------------
# Durable graph (SqliteSaver) — used by the FastAPI /agent/chat endpoint so
# that an interrupted (approval-pending) run survives across requests.
# ---------------------------------------------------------------------------

_durable_graph = None


def get_durable_graph():
    """Lazily build + cache a SqliteSaver-backed graph for the API layer.

    The same compiled instance is reused across requests so that
    ``graph.invoke(Command(resume=...), config)`` can resume an interrupted
    run from a prior request (the interrupt requires a checkpointer).

    NOTE (SqliteSaver is sync-only): ``langgraph-checkpoint-sqlite`` does not
    support async streaming (``astream_events``).  That is why the API keeps
    two paths: (1) this durable graph for non-streaming HITL JSON
    (interrupt/resume across requests), and (2) the module-level in-memory
    ``graph`` (MemorySaver) for ``stream=true`` SSE via ``stream()``.  The
    split is deliberate: streaming is an ephemeral live connection that does
    not need cross-request resumption, and HITL approval is blocking anyway.
    """
    global _durable_graph
    if _durable_graph is None:
        import sqlite3
        from pathlib import Path

        from langgraph.checkpoint.sqlite import SqliteSaver
        db_path = str(Path(settings.STORAGE_DIR) / "agent_checkpoints.db")
        Path(settings.STORAGE_DIR).mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        SqliteSaver(conn).setup()
        _durable_graph = build_graph(checkpointer=SqliteSaver(conn))
    return _durable_graph


def resume_from_approval(thread_id: str, approved: bool, employee_id: str | None = None) -> dict:
    """Resume an interrupted graph run with the human's approval decision.

    This is the server-side counterpart of the ``propose_action`` node's
    ``interrupt()``: the API endpoint calls this after the user clicks
    "Đồng ý" / "Hủy" on the approval card.  Returns the final state dict.
    """
    g = get_durable_graph()
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    resume_payload = {"approved": bool(approved), "employee_id": employee_id}
    return g.invoke(Command(resume=resume_payload), config)



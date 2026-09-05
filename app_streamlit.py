"""MAIA — Enterprise Employee Assistant (AI Receptionist) Streamlit UI."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

try:
    import streamlit as st
    if hasattr(st, "secrets"):
        for k, v in st.secrets.items():
            if isinstance(v, str) and k not in os.environ:
                os.environ[k] = v
except Exception:
    pass

import streamlit as st

from maia.config import settings
from maia.pipeline_query import ingest_data_dir, build_stack

st.set_page_config(page_title="MAIA — Enterprise Employee Assistant", layout="wide")
st.title("MAIA — Enterprise Employee Assistant")
st.caption("AI Receptionist · HR / IT / Security / VPN / Expense — RAG + Tools + Grounding + Citations")

with st.sidebar:
    st.header("System")
    if "localhost" in settings.QDRANT_URL:
        st.warning("QDRANT_URL is localhost — set secrets for cloud deploy.")
    try:
        _, store, _, reranker, llm = build_stack()
        st.metric("Qdrant points", store.count())
        st.write(f"Collection: `{settings.QDRANT_COLLECTION}`")
        st.write(f"LLM: `{settings.CLOUDFLARE_MODEL}` (mode={llm.mode})")
        st.write(f"Rerank: {reranker.mode}")
    except Exception as e:
        st.error(f"Backend error: {e}")
    st.divider()
    st.subheader("Ingest")
    if st.button("Re-ingest data/samples"):
        with st.spinner("Ingesting samples..."):
            st.json(ingest_data_dir())
    if st.button("Re-ingest enterprise docs"):
        with st.spinner("Ingesting enterprise..."):
            st.json(ingest_data_dir(settings.ENTERPRISE_DATA_DIR))
    uploaded = st.file_uploader("Upload docs (.md/.txt/.pdf)", accept_multiple_files=True)
    if uploaded and st.button("Ingest uploads"):
        dest = Path(settings.DATA_DIR)
        dest.mkdir(parents=True, exist_ok=True)
        for f in uploaded:
            (dest / f.name).write_bytes(f.read())
        with st.spinner("Ingesting..."):
            st.json(ingest_data_dir(str(dest)))
    st.divider()
    employee_id = st.text_input("Employee ID", value=settings.DEFAULT_EMPLOYEE_ID)
    session_id = st.text_input("Session ID", value="streamlit_default")
    if st.button("Clear chat history"):
        from maia.agent.session import session_store
        session_store.clear(session_id)
        st.session_state.pop("messages", None)
        st.success(f"Cleared {session_id}")

if "messages" not in st.session_state:
    st.session_state.messages = []

# Quick demo buttons
col1, col2, col3, col4 = st.columns(4)
demo_q = None
if col1.button("📄 Nghỉ phép policy"):
    demo_q = "Chính sách nghỉ phép như thế nào?"
if col2.button("✈️ Xin nghỉ 5 ngày"):
    demo_q = "Tôi muốn xin nghỉ phép 5 ngày từ 10/09"
if col3.button("💻 Mất laptop"):
    demo_q = "Tôi làm mất laptop công ty. Tôi cần làm gì?"
if col4.button("🔐 Request VPN"):
    demo_q = "Cách request VPN?"

# Chat history display
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("citations"):
            with st.expander(f"Sources ({len(m['citations'])})"):
                for c in m["citations"]:
                    st.markdown(f"**{c['tag']} {c['filename']}** (dense={c['dense_score']:.3f} rerank={c['rerank_score']:.3f})")
                    st.write(c["text"])
        if m.get("action"):
            st.json(m["action"])

# Input
prompt = st.chat_input("Hỏi MAIA — VD: Tôi muốn biết chính sách nghỉ phép / Laptop bị hỏng liên hệ ai? / Cách request VPN?")
if demo_q:
    prompt = demo_q

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("MAIA đang trả lời..."):
            from maia.agent.agent import EnterpriseAgent
            agent = EnterpriseAgent()
            res = agent.chat(prompt, session_id=session_id, employee_id=employee_id)
        if not res.get("has_evidence") and not res.get("action") and not res.get("needs_clarification"):
            st.warning("Không đủ bằng chứng trong knowledge base — cân nhắc bổ sung tài liệu.")
        st.markdown(res["answer"])
        # intent badge
        st.caption(f"Intent: `{res.get('intent')}` · Grounding: {res.get('grounding_score')} · Cites valid: {res.get('cites_valid')} · LLM: {res.get('llm_mode')}")
        if res.get("action"):
            st.success(f"Action: {res['action'].get('type')}")
            st.json(res["action"]["result"] if isinstance(res["action"].get("result"), dict) else res["action"])
        if res.get("citations"):
            with st.expander(f"Sources ({len(res['citations'])})"):
                for c in res["citations"]:
                    st.markdown(f"**{c['tag']} {c['filename']}** (dense={c['dense_score']:.3f} rerank={c['rerank_score']:.3f})")
                    st.write(c["text"])
                    st.json({k: c[k] for k in ("chunk_id", "section") if k in c})
        # save to history
        st.session_state.messages.append({"role": "assistant", "content": res["answer"], "citations": res.get("citations", []), "action": res.get("action")})

st.divider()
with st.expander("Legacy: Single-turn RAG query"):
    q = st.text_input("Ask (legacy):", "MAIA RAG pipeline gồm những bước nào?")
    top_k = st.slider("top-k final", 1, 8, settings.TOP_K_FINAL)
    if st.button("Ask legacy"):
        with st.spinner("Retrieving..."):
            from maia.pipeline_query import query
            res = query(q, top_k_final=top_k)
        st.write(res["answer"])
        for c in res.get("citations", []):
            with st.expander(f"{c['tag']} {c['filename']}"):
                st.write(c["text"])

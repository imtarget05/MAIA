"""MAIA Streamlit UI - Full + citation/evidence display (verify: citation)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Map Streamlit Cloud secrets (.streamlit/secrets.toml) -> env vars
# so maia.config (pydantic-settings) picks them up. Local .env still works.
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
from maia.pipeline_query import ingest_data_dir, query, build_stack

st.set_page_config(page_title="MAIA — Intelligent RAG Knowledge Platform", layout="wide")
st.title("MAIA — Intelligent RAG Knowledge Platform")
st.caption("Documents → Chunking → HF Embeddings → Qdrant → Hybrid + Rerank → Cloudflare LLM → Grounded Answer")

with st.sidebar:
    st.header("System")
    if "localhost" in settings.QDRANT_URL:
        st.error(
            "⚠️ QDRANT_URL is still localhost — Streamlit secrets were not loaded. "
            "Go to app → Settings → Secrets and add QDRANT_URL / QDRANT_API_KEY."
        )
    try:
        _, store, _, reranker, llm = build_stack()
        st.metric("Qdrant points", store.count())
        st.write(f"Collection: `{settings.QDRANT_COLLECTION}`")
        st.write(f"Qdrant: `{settings.QDRANT_URL}`")
        st.write(f"Embed: `{settings.EMBED_MODEL}` ({llm.mode})")
        st.write(f"LLM: `{settings.CLOUDFLARE_MODEL}` (mode={llm.mode})")
        st.write(f"Rerank: {reranker.mode}")
    except Exception as e:
        st.error(f"Backend error: {e}")
    if st.button("Re-ingest data/samples"):
        with st.spinner("Ingesting..."):
            st.json(ingest_data_dir())
    uploaded = st.file_uploader("Upload docs (.md/.txt/.pdf)", accept_multiple_files=True)
    if uploaded and st.button("Ingest uploads"):
        dest = Path(settings.DATA_DIR)
        dest.mkdir(parents=True, exist_ok=True)
        for f in uploaded:
            (dest / f.name).write_bytes(f.read())
        with st.spinner("Ingesting..."):
            st.json(ingest_data_dir(str(dest)))

q = st.text_input("Ask your knowledge base:", "MAIA RAG pipeline gồm những bước nào?")
top_k = st.slider("top-k final", 1, 8, settings.TOP_K_FINAL)
if st.button("Ask", type="primary") and q:
    with st.spinner("Retrieving + generating..."):
        res = query(q, top_k_final=top_k)
    if not res.get("has_evidence"):
        st.warning("Không đủ bằng chứng trong knowledge base — câu trả lời có thể không đáng tin.")
    st.subheader("Answer")
    st.write(res["answer"])
    st.subheader("Evidence / Citations")
    for c in res.get("citations", []):
        with st.expander(f"{c['tag']} {c['filename']} (dense={c['dense_score']:.3f}, rerank={c['rerank_score']:.3f})"):
            st.write(c["text"])
            st.json({k: c[k] for k in ("chunk_id", "page", "section")})

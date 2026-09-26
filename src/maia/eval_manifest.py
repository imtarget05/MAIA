"""WS7 — MLOps run manifest (lightweight, MLflow-free).

Every ``maia.eval`` / benchmark run can emit a manifest JSON that records the
exact conditions of the run so results are reproducible and comparable:

    {run_id, timestamp, git_sha, mode ("hash"|"fastembed"), embed_model,
     embed_dim, top_k, thresholds, flags, dataset, metrics}

Usage (CLI)::

    python -m maia.eval --all --top-k 3 --manifest storage/manifests/

or programmatically::

    from maia.eval_manifest import build_manifest, write_manifest
    m = build_manifest(metrics={...}, dataset="eval/golden/", top_k=3)
    write_manifest(m, Path("storage/manifests"))
"""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _git_sha() -> str:
    """Best-effort short git sha; 'unknown' outside a repo."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, timeout=5
        ).strip()
    except Exception:
        return "unknown"


def embed_mode() -> str:
    """Which embedder mode the run used: 'hash' (lower bound) or 'fastembed'."""
    import os
    if os.environ.get("MAIA_EMBED_FORCE_HASH") == "1":
        return "hash"
    try:
        from maia.embeddings import Embedder
        return getattr(Embedder(), "mode", "fastembed")
    except Exception:
        return "unknown"


def _runtime_embed_dim() -> int:
    """Vector width the running pipeline actually uses."""
    try:
        from maia.embeddings import get_embedder

        return int(get_embedder().dim)
    except Exception:
        from maia.config import settings

        return int(settings.CLOUDFLARE_EMBED_DIM)


def build_manifest(*, metrics: dict, dataset: str, top_k: int = 3,
                   extra: dict | None = None) -> dict:
    """Assemble the run manifest from settings + environment."""
    from maia.config import settings
    manifest = {
        "run_id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "mode": embed_mode(),
        "embed_model": settings.EMBED_MODEL,
        # settings.EMBED_DIM describes the offline fallbacks; the dim actually
        # used in this run is the embedder's, which is 1024 on the Cloudflare
        # BGE-M3 path. Recording the wrong one made the manifest useless for
        # reproducing a run.
        "embed_dim": _runtime_embed_dim(),
        "fallback_embed_dim": settings.EMBED_DIM,
        "top_k": top_k,
        "thresholds": {
            "similarity": getattr(settings, "SIMILARITY_THRESHOLD", None),
            "grounding": getattr(settings, "GROUNDING_THRESHOLD", None),
        },
        "flags": {
            "LLAMA_INDEX_DATA_PLANE": settings.LLAMA_INDEX_DATA_PLANE,
            "CRAG_ENABLED": getattr(settings, "CRAG_ENABLED", None),
            "LTM_ENABLED": settings.LTM_ENABLED,
            "LTM_LEARN_ON_CHAT": settings.LTM_LEARN_ON_CHAT,
        },
        "dataset": dataset,
        "metrics": metrics,
    }
    if extra:
        manifest.update(extra)
    return manifest


def write_manifest(manifest: dict, out_dir: str | Path) -> Path:
    """Persist the manifest as JSON; returns the written path."""
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    path = p / f"eval_run_{manifest.get('run_id', 'na')}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return path

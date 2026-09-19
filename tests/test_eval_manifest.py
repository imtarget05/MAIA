"""WS7 — MLOps run manifest tests (offline, no Qdrant)."""
import json


def test_manifest_roundtrip(tmp_path):
    """WS7: build_manifest captures settings/env, write_manifest persists JSON."""
    from maia.eval_manifest import build_manifest, write_manifest
    m = build_manifest(metrics={"hit@k": 1.0}, dataset="eval/golden/vi_policy.jsonl",
                       top_k=3)
    assert m["mode"] in ("hash", "fastembed", "unknown")
    assert m["metrics"] == {"hit@k": 1.0}
    assert m["flags"]["LLAMA_INDEX_DATA_PLANE"] in (True, False)
    assert "run_id" in m and "timestamp" in m
    p = write_manifest(m, tmp_path)
    assert p.exists()
    loaded = json.loads(p.read_text())
    assert loaded["run_id"] == m["run_id"]
    assert loaded["embed_dim"] >= 1


def test_manifest_git_sha_graceful():
    from maia.eval_manifest import _git_sha
    assert isinstance(_git_sha(), str) and len(_git_sha()) >= 7 or _git_sha() == "unknown"

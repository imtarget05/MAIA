"""NER tool backend: BiLSTM PhoNER_COVID19 tagger with lazy torch load.

Loaded lazily (singleton cache) so the main venv works without torch:
missing torch / checkpoint returns {"ok": False, ...} instead of crashing.
BIO decode mirrors ner/ie_retrieval_demo.py:extract().
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

MODEL_NAME = "bilstm-phoner"

_SEG = "_"

_bundle = None  # cached (vocab, labels, model) or {"error": ...}


def _ckpt_dir() -> Path:
    override = os.environ.get("MAIA_NER_CKPT_DIR")
    if override:
        return Path(override)
    # src/maia/ner_tool.py -> repo root = parents[2]
    return Path(__file__).resolve().parents[2] / "ner" / "checkpoints-bilstm"


def _normalize(w: str) -> str:
    w = w.replace(_SEG, " ")
    if re.fullmatch(r"[0-9.,/%-]+", w):
        return "<NUM>"
    return w.lower()


def _reset_cache() -> None:
    """Test helper: clear the singleton cache."""
    global _bundle
    _bundle = None


def _build_model_from_state_dict(torch, state: dict):
    """Rebuild emb + 1-layer BiLSTM + linear tagger inferring dims from weights."""
    v, e = state["emb.weight"].shape
    h = state["lstm.weight_ih_l0"].shape[0] // 4
    c = state["fc.weight"].shape[0]
    bidir = "lstm.weight_ih_l0_reverse" in state

    class _Tagger(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.emb = torch.nn.Embedding(v, e, padding_idx=0)
            self.lstm = torch.nn.LSTM(e, h, 1, batch_first=True,
                                      bidirectional=bidir)
            self.drop = torch.nn.Dropout(0.0)
            self.fc = torch.nn.Linear(h * (2 if bidir else 1), c)

        def forward(self, x):
            h_out, _ = self.lstm(self.emb(x))
            return self.fc(self.drop(h_out))

    model = _Tagger()
    model.load_state_dict(state)
    return model


def _load_bundle():
    """Lazy-load vocab/labels/model. Returns (vocab, labels, model) or raises."""
    global _bundle
    if _bundle is not None:
        if isinstance(_bundle, dict) and "error" in _bundle:
            raise RuntimeError(_bundle["error"])
        return _bundle

    try:
        import torch  # lazy: main venv has no torch
    except Exception as e:
        _bundle = {"error": f"torch not available: {e}"}
        raise RuntimeError(_bundle["error"])

    ckpt = _ckpt_dir()
    for f in ("vocab.json", "labels.json", "model.pt"):
        if not (ckpt / f).exists():
            _bundle = {"error": f"NER checkpoint missing: {ckpt / f}"}
            raise RuntimeError(_bundle["error"])
    try:
        vocab = json.loads((ckpt / "vocab.json").read_text(encoding="utf-8"))
        labels = json.loads((ckpt / "labels.json").read_text(encoding="utf-8"))
        import sys

        sys.path.insert(0, str(ckpt.parent))
        try:
            from bilstm import BiLSTMTagger

            model = BiLSTMTagger(len(vocab), c=len(labels))
            model.load_state_dict(torch.load(str(ckpt / "model.pt"), map_location="cpu"))
        except Exception:
            # ner/bilstm.py may be unimportable (e.g. missing dataset.py);
            # rebuild the same arch (emb + 1-layer BiLSTM + linear) with dims
            # inferred from the state_dict, then load weights.
            model = _build_model_from_state_dict(
                torch, torch.load(str(ckpt / "model.pt"), map_location="cpu")
            )
        model.eval()
    except Exception as e:
        _bundle = {"error": f"NER checkpoint load failed: {e}"}
        raise RuntimeError(_bundle["error"])
    _bundle = (vocab, labels, model)
    return _bundle


def _decode_bio(words: list[str], pred_ids: list[int], labels: list[str]) -> list[dict]:
    ents, cur = [], None
    for w, lid in zip(words, pred_ids):
        lab = labels[lid] if 0 <= lid < len(labels) else "O"
        if lab.startswith("B-"):
            if cur:
                ents.append(cur)
            cur = {"type": lab[2:], "tokens": [w]}
        elif lab.startswith("I-") and cur and cur["type"] == lab[2:]:
            cur["tokens"].append(w)
        else:
            if cur:
                ents.append(cur)
            cur = None
    if cur:
        ents.append(cur)
    return [{"type": e["type"], "text": " ".join(e["tokens"])} for e in ents]


def extract_entities(text: str, max_tokens: int = 64) -> dict:
    """Extract PhoNER entities with the BiLSTM checkpoint.

    Returns {"ok": True, "entities": [...], "model": ...} or
    {"ok": False, "error": ...} when torch/checkpoint is unavailable.
    """
    if not text or not text.split():
        return {"ok": True, "entities": [], "model": MODEL_NAME}
    try:
        vocab, labels, model = _load_bundle()
        import torch

        words = text.split()[:max(1, max_tokens)]
        ids = torch.tensor(
            [[vocab.get(_normalize(w), vocab["<UNK>"]) for w in words]]
        )
        with torch.no_grad():
            pred = model(ids).argmax(-1)[0].tolist()
        return {
            "ok": True,
            "entities": _decode_bio(words, pred, labels),
            "model": MODEL_NAME,
        }
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"NER inference failed: {e}"}


def filter_docs_by_entities(docs: list[str], entities: list[dict]) -> list[str]:
    """MUST-match entity filter (same semantics as ner/ie_retrieval_demo.py).

    Keeps docs containing every entity text (case-insensitive); falls back
    to the unfiltered list when the filter would return empty.
    """
    must = {str(e.get("text", "")).lower() for e in (entities or []) if e.get("text")}
    must.discard("")
    if not must or not docs:
        return list(docs)
    filt = [d for d in docs if all(m in d.lower() for m in must)]
    return filt or list(docs)

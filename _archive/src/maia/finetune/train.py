"""Embedding fine-tune runner (LoRA-free, sentence-transformers).

Requires torch + sentence-transformers + a GPU for anything real: this runner
imports them lazily and returns an actionable error dict when absent, so
`import maia.finetune` stays light and offline-safe. The export step
(export.py) is the part MAIA runs locally; training happens where GPUs live.
"""
from __future__ import annotations


def check_deps() -> dict:
    """Report whether training dependencies are importable (no side effects)."""
    missing = []
    for mod in ("torch", "sentence_transformers", "datasets"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    return {"ok": not missing, "missing": missing,
            "hint": "pip install torch sentence-transformers datasets" if missing else ""}


def train(triplets_path: str, base_model: str = "BAAI/bge-m3",
          output_dir: str = "storage/finetune/model", epochs: int = 1,
          batch_size: int = 8) -> dict:
    """Fine-tune `base_model` with MultipleNegativesRankingLoss on triplets.

    Returns {"ok": True, "output_dir": ...} or {"ok": False, "error": ...}.
    """
    deps = check_deps()
    if not deps["ok"]:
        return {"ok": False, "error": "missing_deps", **deps}
    try:
        import json

        from datasets import Dataset as HFDataset
        from sentence_transformers import InputExample, SentenceTransformer, losses
        from sentence_transformers import datasets as st_datasets
        from torch.utils.data import DataLoader

        rows = [json.loads(l) for l in open(triplets_path).read().splitlines() if l.strip()]
        if not rows:
            return {"ok": False, "error": "empty_triplets", "path": triplets_path}
        examples = []
        for r in rows:
            examples.append(InputExample(texts=[r["anchor"], r["positive"]]))
            for n in r.get("negatives", [])[:1]:
                examples.append(InputExample(texts=[r["anchor"], n]))
        model = SentenceTransformer(base_model)
        loader = DataLoader(examples, shuffle=True, batch_size=batch_size)
        loss = losses.MultipleNegativesRankingLoss(model)
        model.fit([(loader, loss)], epochs=epochs, output_path=output_dir,
                  show_progress_bar=False)
        model.save(output_dir)
        _ = HFDataset  # re-export guard: datasets required for HF-hub flows
        _ = st_datasets
        return {"ok": True, "output_dir": output_dir, "examples": len(examples),
                "base_model": base_model, "epochs": epochs}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

"""Entity-level eval for the BiLSTM checkpoint on PhoNER test split.
Usage: ./ner/.venv/bin/python ner/bilstm_eval.py [--max-test 0]
Writes eval/ner-report-<date>.json (same schema as ner/eval.py).
"""
import argparse
import json
import os
import sys
from datetime import date

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bilstm import BiLSTMTagger, NERDataset, collate, normalize  # noqa: E402
from dataset import load_split  # noqa: E402

CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints-bilstm")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-test", type=int, default=0)
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()

    vocab = json.load(open(os.path.join(CKPT_DIR, "vocab.json")))
    labs = json.load(open(os.path.join(CKPT_DIR, "labels.json")))
    model = BiLSTMTagger(len(vocab), c=len(labs))
    model.load_state_dict(torch.load(os.path.join(CKPT_DIR, "model.pt"), map_location="cpu"))
    model.eval()

    rows = load_split("test")
    if args.max_test:
        rows = args.max_test and rows[:args.max_test]
    tag2id = {t: i for i, t in enumerate(labs)}
    from torch.utils.data import DataLoader
    dl = DataLoader(NERDataset(rows, vocab, tag2id), batch_size=args.batch, collate_fn=collate)

    yt, yp = [], []
    with torch.no_grad():
        for X, Y, M in dl:
            pred = model(X).argmax(-1).tolist()
            for p, y, m in zip(pred, Y.tolist(), M.tolist()):
                a, b = [], []
                for pi, yi, mi in zip(p, y, m):
                    if mi < 0.5:
                        break
                    a.append(labs[pi])
                    b.append(labs[yi])
                yp.append(a)
                yt.append(b)
    from seqeval.metrics import classification_report
    rep = classification_report(yt, yp, output_dict=True, zero_division=0)
    per_type = {k: round(v["f1-score"], 3) for k, v in rep.items()
                if k not in ("accuracy", "macro avg", "micro avg", "weighted avg")}
    report = {
        "date": str(date.today()),
        "model": "BiLSTM-emb128-h128 greedy (Lample-2016 minus CRF), CPU-trained",
        "n_test": len(rows),
        "micro_f1": round(rep["micro avg"]["f1-score"], 4),
        "macro_f1": round(rep["macro avg"]["f1-score"], 4),
        "precision_micro": round(rep["micro avg"]["precision"], 4),
        "recall_micro": round(rep["micro avg"]["recall"], 4),
        "per_type_f1": per_type,
        "paper_reference": "Nguyen et al. 2020 PhoNER_COVID19: PhoBERT-large 0.945 test micro-F1 (transformer SOTA); this CPU baseline is expected lower — see MODEL_CARD.md",
    }
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "eval", f"ner-report-{date.today()}.json")
    json.dump(report, open(out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

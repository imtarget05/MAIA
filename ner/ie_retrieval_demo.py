"""IE -> retrieval story (Cazoodle "information extraction + retrieval" evidence).

For each demo query: BiLSTM-NER extracts entities -> entities become MUST-match
keyword filters on the retrieval candidate list -> report shows that
entity-filtering raises keyword-precision on the returned context.

Runs on the REAL trained checkpoint (ner/checkpoints-bilstm). Retrieval side
uses BM25 over PhoNER dev sentences (same domain), fully offline.

Usage: ./ner/.venv/bin/python ner/ie_retrieval_demo.py
Writes: eval/ie-retrieval-demo-<date>.json
"""
import json
import os
import sys
from datetime import date

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bilstm import BiLSTMTagger, normalize  # noqa: E402
from dataset import load_split  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints-bilstm")
QUERIES = [
    "Bệnh viện Bạch Mai ở Hà Nội tiếp nhận bệnh nhân COVID-19",
    "Bệnh nhân 35 tuổi làm việc tại chợ Đồng Xuân có triệu chứng sốt cao",
    "Chuyến bay VN118 từ Đà Nẵng hạ cánh xuống Tân Sơn Nhất",
]


def extract(text: str, vocab: dict, labs: list[str], model) -> list[dict]:
    ids = torch.tensor([[vocab.get(normalize(w), vocab["<UNK>"]) for w in text.split()[:64]]])
    with torch.no_grad():
        pred = model(ids).argmax(-1)[0].tolist()
    words = text.split()[:64]
    ents, cur = [], None
    for w, lid in zip(words, pred):
        lab = labs[lid]
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


def main() -> int:
    vocab = json.load(open(os.path.join(CKPT_DIR, "vocab.json")))
    labs = json.load(open(os.path.join(CKPT_DIR, "labels.json")))
    model = BiLSTMTagger(len(vocab), c=len(labs))
    model.load_state_dict(torch.load(os.path.join(CKPT_DIR, "model.pt"), map_location="cpu"))
    model.eval()

    dev = load_split("dev")
    corpus = [" ".join(r["words"]).replace("_", " ") for r in dev]
    bm25 = BM25Okapi([c.lower().split() for c in corpus])

    cases = []
    for q in QUERIES:
        ents = extract(q, vocab, labs, model)
        must = {e["text"].lower() for e in ents}
        scores = bm25.get_scores(q.lower().split())
        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:5]
        base = [corpus[i] for i in top_idx if scores[i] > 0]
        filt = [c for c in base if all(m in c.lower() for m in must)] or base
        kw = [k for k in q.lower().split() if len(k) > 2]

        def prec(docs):
            return round(sum(1 for d in docs if any(k in d.lower() for k in kw)) / max(1, len(docs)), 3)

        cases.append({"query": q, "entities": ents, "bm25_top5": base,
                      "entity_filtered": filt, "precision_base": prec(base),
                      "precision_filtered": prec(filt)})
    report = {"date": str(date.today()), "model": "checkpoints-bilstm", "cases": cases,
              "note": "entities from the trained BiLSTM act as retrieval filters; "
                      "compare precision_base vs precision_filtered per query."}
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "eval", f"ie-retrieval-demo-{date.today()}.json")
    json.dump(report, open(dest, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1)[:2500])
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

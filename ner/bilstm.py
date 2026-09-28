"""BiLSTM tagger (greedy, CRF-less) for PhoNER — CPU-budget baseline.

Why this exists alongside train.py (DistilBERT fine-tune): the dev box is a
dual-core Intel Mac under swap pressure (~150s/step for 134M-param
transformers). This BiLSTM (word emb 128 + 1-layer BiLSTM 128, ~2M params)
trains to a REAL baseline in minutes on the same data/splits, evaluated with
the same seqeval harness. Architecture follows Lample et al. 2016 minus the
CRF layer (greedy BIO decode) — documented, not hidden.

Usage: ./ner/.venv/bin/python ner/bilstm.py [--epochs 8] [--max-train 0]
Saves: ner/checkpoints-bilstm/{model.pt, vocab.json, labels.json, log.json}
"""
import argparse
import json
import os
import re
import time

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataset import label_list, load_split  # noqa: E402

CKPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkpoints-bilstm")
SEG = "_"  # PhoNER word-level uses underscores for segmented words


def normalize(w: str) -> str:
    w = w.replace(SEG, " ")
    if re.fullmatch(r"[0-9.,/%-]+", w):
        return "<NUM>"
    return w.lower()


class NERDataset(Dataset):
    def __init__(self, rows, vocab, tag2id, max_len=64):
        self.rows, self.vocab, self.tag2id, self.max_len = rows, vocab, tag2id, max_len

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        r = self.rows[i]
        words = [normalize(w) for w in r["words"][:self.max_len]]
        tags = r["tags"][:self.max_len]
        ids = [self.vocab.get(w, self.vocab["<UNK>"]) for w in words]
        labs = [self.tag2id[t] for t in tags]
        return torch.tensor(ids), torch.tensor(labs), len(ids)


def collate(batch):
    xs, ys, ns = zip(*batch)
    n = max(ns)
    X = torch.zeros(len(batch), n, dtype=torch.long)
    Y = torch.full((len(batch), n), -100, dtype=torch.long)
    M = torch.zeros(len(batch), n)
    for i, (x, y, l) in enumerate(zip(xs, ys, ns)):
        X[i, :l], Y[i, :l], M[i, :l] = x, y, 1.0
    return X, Y, M


class BiLSTMTagger(nn.Module):
    def __init__(self, v, e=128, h=128, c=20, drop=0.3):
        super().__init__()
        self.emb = nn.Embedding(v, e, padding_idx=0)
        self.lstm = nn.LSTM(e, h, 1, batch_first=True, bidirectional=True)
        self.drop = nn.Dropout(drop)
        self.fc = nn.Linear(h * 2, c)

    def forward(self, x):
        h, _ = self.lstm(self.emb(x))
        return self.fc(self.drop(h))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--max-train", type=int, default=0)
    ap.add_argument("--max-dev", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    torch.manual_seed(args.seed)

    train_rows = load_split("train")
    dev_rows = load_split("dev")
    if args.max_train:
        train_rows = train_rows[:args.max_train]
    if args.max_dev:
        dev_rows = dev_rows[:args.max_dev]
    labs = label_list(train_rows + dev_rows)
    tag2id = {t: i for i, t in enumerate(labs)}

    vocab = {"<PAD>": 0, "<UNK>": 1}
    for r in train_rows:
        for w in r["words"]:
            w = normalize(w)
            if w not in vocab:
                vocab[w] = len(vocab)
    print(f"vocab={len(vocab)} labels={len(labs)} train={len(train_rows)} dev={len(dev_rows)}", flush=True)

    model = BiLSTMTagger(len(vocab), c=len(labs))
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    loss_fn = nn.CrossEntropyLoss(ignore_index=-100)
    train_dl = DataLoader(NERDataset(train_rows, vocab, tag2id), batch_size=args.batch,
                          shuffle=True, collate_fn=collate)
    dev_dl = DataLoader(NERDataset(dev_rows, vocab, tag2id), batch_size=64, collate_fn=collate)

    def run_eval():
        model.eval()
        yt, yp = [], []
        with torch.no_grad():
            for X, Y, M in dev_dl:
                pred = model(X).argmax(-1)
                for p, y, m in zip(pred.tolist(), Y.tolist(), M.tolist()):
                    a, b = [], []
                    for pi, yi, mi in zip(p, y, m):
                        if mi < 0.5:
                            break
                        a.append(labs[pi])
                        b.append(labs[yi])
                    yp.append(a)
                    yt.append(b)
        from seqeval.metrics import f1_score
        return f1_score(yt, yp)

    t0 = time.time()
    for ep in range(args.epochs):
        model.train()
        tot, n = 0.0, 0
        for X, Y, M in train_dl:
            opt.zero_grad()
            loss = loss_fn(model(X).transpose(1, 2), Y)
            loss.backward()
            opt.step()
            tot += loss.item()
            n += 1
        f1 = run_eval()
        print(f"epoch {ep+1}/{args.epochs} loss={tot/max(1,n):.4f} dev_microF1={f1:.4f} "
              f"elapsed={time.time()-t0:.0f}s", flush=True)
    os.makedirs(CKPT_DIR, exist_ok=True)
    torch.save(model.state_dict(), os.path.join(CKPT_DIR, "model.pt"))
    json.dump(vocab, open(os.path.join(CKPT_DIR, "vocab.json"), "w"))
    json.dump(labs, open(os.path.join(CKPT_DIR, "labels.json"), "w"))
    json.dump({"epochs": args.epochs, "batch": args.batch, "lr": args.lr,
               "n_train": len(train_rows), "n_dev": len(dev_rows),
               "dev_microF1": f1, "arch": "emb128+BiLSTM128 greedy (Lample-2016 minus CRF)",
               "elapsed_s": round(time.time() - t0)}, 
              open(os.path.join(CKPT_DIR, "log.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

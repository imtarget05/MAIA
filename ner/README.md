# NER — Vietnamese Information Extraction (Cazoodle evidence)

**Shipped:** BiLSTM tagger, test micro-F1 **0.8325** (n=3000) — `eval/ner-report-2026-09-28.json`,
MODEL_CARD `ner/MODEL_CARD.md`. **DistilBERT-multilingual (Colab T4, xong):**
test micro-F1 **0.9091** (n=3000, report `eval/ner-report-distilbert-2026-09-28.json`),
local verify n=1000 → 0.8694. Code train trong `ner/colab_train_distilbert.ipynb`.

Dataset **PhoNER_COVID19** (Nguyen et al. 2020): 5027 train / 2000 dev /
3000 test sents, word-level BIO, 10 entity types.

## Tại sao DistilBERT-multilingual, không phải PhoBERT?
Box dev là Intel Mac CPU-only; PhoBERT cần word-segmentation (VnCoreNLP) ở
inference. mDistilBERT nhận raw text (WordPiece) — baseline chính đáng trong
paper (mBERT ~0.90 test F1; PhoBERT-large 0.945).

## Chạy
```
./ner/.venv/bin/python ner/bilstm.py        # train BiLSTM (~15 min CPU) -> ner/checkpoints-bilstm/
./ner/.venv/bin/python ner/bilstm_eval.py   # seqeval test eval -> eval/ner-report-<date>.json
./ner/.venv/bin/python ner/ie_retrieval_demo.py  # IE->retrieval story -> eval/ie-retrieval-demo-<date>.json
# DistilBERT fine-tune: mở ner/colab_train_distilbert.ipynb trên Colab (T4),
# chạy 9 cells (code đã nhúng sẵn trong notebook, không cần upload gì).
# Kết quả tải về: checkpoints/ -> ner/checkpoints/, report -> eval/
```

## Files
- `colab_train_distilbert.ipynb` — toàn bộ pipeline transformers (dataset/train/eval/infer nhúng trong notebook, chạy Colab T4)
- `bilstm.py` / `bilstm_eval.py` — BiLSTM CPU baseline (train + seqeval eval)
- `MODEL_CARD.md` — data card + hyperparams + kết quả + limitation
- `ie_retrieval_demo.py` — câu chuyện IE→retrieval: entity từ query dùng làm filter cho HybridRetriever

venv riêng (`ner/.venv`, Python 3.11 + torch 2.2.2) vì torch không còn wheel
macOS x86_64 cho Python 3.13/3.14 — xem `requirements.txt`.

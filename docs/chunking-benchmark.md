# Chunking benchmark (2026-09-28, data/enterprise 8 .md, offline)

Command: `PYTHONPATH=src .venv/bin/python scripts/bench_chunking.py`

| strategy | size | chunks | avg chars | max chars | time |
|---|---|---|---|---|---|
| sentence (LlamaIndex) | 512 | 25 | 812 | 1166 | 26.3s (first run incl. init) |
| sentence (LlamaIndex) | 1024 | 13 | 1494 | 2252 | 0.11s |
| paragraph (stdlib) | 512 | 85 | 517 | 1047 | ~0s |
| paragraph (stdlib) | 1024 | 35 | 816 | 1047 | ~0s |

Notes (honest):

- LlamaIndex `SentenceSplitter(chunk_size=)` counts **tokens**, not chars — hence
  avg 812 chars at size=512. Paragraph strategy counts chars and never cuts a
  paragraph mid-way.
- Default stays `sentence/512` (tested path, fewer chunks = cheaper retrieval).
  Use `paragraph/1024` when headings must stay intact (policy docs with tables).
- `split_documents(..., strategy="paragraph")` is the stdlib fallback path that
  also works when LlamaIndex is not installed.

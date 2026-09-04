#!/bin/bash
# MAIA demo: ingest -> query -> eval
set -e
cd "$(dirname "$0")"
PYTHONPATH=src python -m maia.cli ingest
PYTHONPATH=src python -m maia.cli query "MAIA RAG pipeline gồm những bước nào?"
PYTHONPATH=src python -m maia.eval eval/dataset.jsonl


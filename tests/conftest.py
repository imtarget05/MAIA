"""Pytest session config: force the FULL test suite to run fully offline.

* `MAIA_EMBED_FORCE_HASH=1` -> deterministic hash embedding (no HF Hub model
  download, no network) so tests never depend on external services.
* Tests must NOT require a running Qdrant / Kafka / localhost service — they run
  against the in-memory broker + in-memory vector store (see tests/test_stream.py).

Set before maia.* modules are imported so the Embedder picks hash mode.
"""
import os

os.environ["MAIA_EMBED_FORCE_HASH"] = "1"
"""Pytest session config: force the FULL test suite to run fully offline.

* `MAIA_EMBED_FORCE_HASH=1` -> deterministic hash embedding (no HF Hub model
  download, no network) so tests never depend on external services.
* Tests must NOT require a running Qdrant / Kafka / localhost service — they run
  against the in-memory broker + in-memory vector store (see tests/test_stream.py).

Set before maia.* modules are imported so the Embedder picks hash mode.
"""
import os

os.environ["MAIA_EMBED_FORCE_HASH"] = "1"


# ---------------------------------------------------------------------------
# Process-global state hygiene
# ---------------------------------------------------------------------------
# The reranker is cached per model name in a module-level dict for the lifetime
# of the process (see maia.reranker.get_reranker). Any test that drives a real
# query() therefore warms that cache with a live instance -- ~90MB of
# cross-encoder weights, plus a resident_reranker_mode() of "cross-encoder".
#
# That leaked into later tests. Concretely:
#   tests/test_tracing.py::{test_query_emits_all_7_stages,
#   test_refusal_path_logs_refusal_reason,
#   test_trace_embedded_when_pipeline_trace_enabled,
#   test_trace_not_embedded_by_default}
# each call query() and leave a real Reranker behind. Running any of them
# before tests/test_health_endpoints.py::test_embedder_singleton_reused made
# that test fail on `resident_reranker_mode() == "not-loaded"`, while the test
# passed in isolation. Order-dependent failure, no random seed involved
# (pytest-randomly is not installed, so collection order is deterministic).
#
# reset_reranker_cache() exists precisely for this -- its docstring already
# names this failure mode -- but only tests/test_reranker_lifecycle.py was
# calling it, and only for itself. Ownership of the reset belongs here so no
# individual test can forget it. The assertion in the health test is correct
# and is NOT being relaxed: the pollution is what gets fixed.
import pytest


@pytest.fixture(autouse=True)
def _release_reranker_cache():
    """Drop any reranker instance cached by a previous test, before and after."""
    from maia import reranker as reranker_mod

    reranker_mod.reset_reranker_cache()
    yield
    reranker_mod.reset_reranker_cache()
"""Runtime dependency contract: what the API image installs vs what the code runs.

WHY THIS EXISTS. `requirements.api.txt` is a curated list, and nothing enforced
the relationship between it and the code. That let a real regression ship:

    src/maia/retriever.py imports `rank_bm25` inside `_build_bm25`, inside a
    bare `try/except Exception` that sets `self._bm25 = None`. The runtime
    manifest omitted the package. The app started, /health passed, and every
    request quietly degraded: `retrieve()` gates the sparse leg on
    `self._bm25 is not None`, so RRF received ONE ranking list instead of two.

The symptom is invisible by construction — no exception, no log, no failed test.
Measured evidence of exactly that shape is in
`docs/evidence/baseline-ai-reality/`, where running the real retriever in a venv
built from `requirements.api.txt` showed `bm25_score > 0` on zero results.

This module asserts the CONTRACT, in two directions: every import a request path
depends on must be installable from `requirements.api.txt`, and every package
named there must resolve. Both directions matter — a one-way check passes
happily while a package is missing, and the reverse catches a stale entry.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_MANIFEST = REPO_ROOT / "requirements.api.txt"
SRC = REPO_ROOT / "src"

# Import name -> distribution name in the manifest.
#
# These are NOT derivable by replacing "-" with "_". Measured examples from this
# very manifest: `python-jose` is imported as `jose`, `pyyaml` as `yaml`,
# `qdrant-client` as `qdrant_client`. A test that assumed the mechanical
# mapping reported three phantom packages and one phantom capability, which is
# what this file exists to prevent — so the mapping is written out explicitly
# rather than guessed at the call site.
IMPORT_TO_DISTRIBUTION = {
    "rank_bm25": "rank-bm25",
    "qdrant_client": "qdrant-client",
    "numpy": "numpy",
}

# The reverse mapping, needed for the "no phantom entries" direction. Packages
# not listed here are checked with the mechanical guess, which is correct for
# simple names and is why the result is only asserted when it is meaningful.
DISTRIBUTION_TO_IMPORT = {
    "rank-bm25": "rank_bm25",
    "qdrant-client": "qdrant_client",
    "python-jose": "jose",
    "pyyaml": "yaml",
    "uvicorn": "uvicorn",
    "fastapi": "fastapi",
    "pydantic": "pydantic",
    "pydantic-settings": "pydantic_settings",
    "sqlalchemy": "sqlalchemy",
    "python-multipart": "multipart",
    "passlib": "passlib",
    "langchain-core": "langchain_core",
    # Distributions whose import name is not derivable by replacing "-" with
    # "_". langgraph-checkpoint-postgres installs as a SUBPACKAGE of
    # langgraph (langgraph.checkpoint.postgres), and llama-index-core installs
    # as the top-level `llama_index` namespace it shares with
    # llama-index-readers-file. Without these the contract test resolves
    # "langgraph_checkpoint_postgres" and "llama_index_core", which no module
    # provides, and reports both as phantom packages.
    "langgraph-checkpoint-postgres": "langgraph.checkpoint.postgres",
    "llama-index-core": "llama_index",
    "langgraph": "langgraph",
    "jsonschema": "jsonschema",
    "requests": "requests",
    "numpy": "numpy",
    "pandas": "pandas",
}

# Capabilities MAIA's public surface claims, and why a missing package is a
# silent capability loss rather than a crash.
RUNTIME_CAPABILITIES: dict[str, str] = {
    "rank_bm25": (
        "sparse leg of hybrid retrieval; retriever.py swallows the ImportError "
        "and RRF then fuses a single ranking list"
    ),
    "numpy": "dense vector arithmetic in the retriever and reranker paths",
    "qdrant_client": "the vector store client every retrieval call goes through",
}


def _manifest_distributions() -> set[str]:
    """Distribution names the runtime image installs.

    Parsed, not grepped: only non-comment, non-option lines are package specs,
    and `pkg>=1.0` normalises to `pkg`. A `grep rank-bm25` would also match the
    header comment explaining why the package was once excluded — exactly the
    confusion this file exists to remove.
    """
    dists: set[str] = set()
    for raw in RUNTIME_MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        spec = line.split("#", 1)[0].strip()
        if spec:
            dists.add(re.split(r"[<>=!~\[;\s]", spec, maxsplit=1)[0].strip().lower())
    return dists


@pytest.mark.parametrize("import_name,reason", sorted(RUNTIME_CAPABILITIES.items()))
def test_runtime_capability_is_installed_in_the_api_image(
    import_name: str, reason: str
) -> None:
    """A capability the product claims must be importable in the runtime."""
    present = importlib.util.find_spec(import_name) is not None
    assert present, (
        f"`{import_name}` is not importable — {reason}. If this environment is not "
        f"the API image, check {RUNTIME_MANIFEST.name} for the declaration instead."
    )


@pytest.mark.parametrize("import_name,reason", sorted(RUNTIME_CAPABILITIES.items()))
def test_runtime_capability_is_declared_in_the_manifest(
    import_name: str, reason: str
) -> None:
    """The manifest must declare every claimed capability.

    This is the half that catches the original regression even when the test
    environment happens to have the package installed: a developer's full
    virtualenv can import `rank_bm25` while `requirements.api.txt` — the only
    thing `Dockerfile.api` installs — does not list it.
    """
    dist = IMPORT_TO_DISTRIBUTION.get(import_name, import_name)
    declared = _manifest_distributions()
    assert dist in declared, (
        f"`{import_name}` ({dist}) is imported on a runtime path — {reason} — but "
        f"{RUNTIME_MANIFEST.name} does not declare it. The API image would install "
        f"without it and degrade silently. declared: {sorted(declared)}"
    )


def test_manifest_entries_all_resolve() -> None:
    """Reverse direction: nothing in the manifest is a phantom package.

    A stale entry hides a real regression behind a green file and ships an
    install step that resolves nothing.

    NOTE ON WHAT A FAILURE MEANS. This compares the manifest against whatever
    environment is running the tests, so an unresolvable name has two possible
    causes: the entry is stale, or THIS ENVIRONMENT was not built from
    requirements.api.txt. The message names both, because guessing wrong here
    sends someone to delete a perfectly good dependency.
    """
    unresolved = [
        dist
        for dist in sorted(_manifest_distributions())
        if importlib.util.find_spec(
            DISTRIBUTION_TO_IMPORT.get(dist, dist.replace("-", "_"))
        )
        is None
    ]
    assert not unresolved, (
        f"{RUNTIME_MANIFEST.name} declares package(s) that do not resolve here: "
        f"{unresolved}. Either the entry is stale/renamed, OR this environment was "
        f"not built from that manifest (CI installs it; a partial venv would also "
        f"fail here). Check the venv before deleting the dependency."
    )


def test_retriever_source_import_matches_the_declared_package() -> None:
    """The sparse leg must use the package the manifest declares.

    If someone swaps `rank_bm25` for another BM25 implementation the distribution
    name changes with it. Asserting only "some BM25 is importable" would let the
    manifest and the code drift apart silently.
    """
    source = (SRC / "maia" / "retriever.py").read_text(encoding="utf-8")
    imported = set(
        re.findall(
            r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+\w*BM25", source, re.MULTILINE
        )
    )
    assert imported, (
        "retriever.py no longer imports a BM25 implementation — the sparse leg "
        "was removed or rewritten. Update the manifest and this contract."
    )
    for module in imported:
        top = module.split(".")[0]
        dist = IMPORT_TO_DISTRIBUTION.get(top, top)
        assert dist in _manifest_distributions(), (
            f"retriever.py imports `{module}` but {RUNTIME_MANIFEST.name} does not "
            f"declare `{dist}`."
        )


def test_manifest_parser_is_not_vacuous() -> None:
    """Guard this file's own helper, so a refactor cannot make it a no-op.

    A contract test whose parser returned an empty set would pass every
    assertion vacuously — the same class of defect as the empty `terraform test`
    run this repository already caught once.
    """
    dists = _manifest_distributions()
    assert "rank-bm25" in dists
    assert "fastapi" in dists
    assert "torch" not in dists, "a commented-out package must not appear"
    assert len(dists) >= 15, f"manifest parse looks truncated: {sorted(dists)}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))

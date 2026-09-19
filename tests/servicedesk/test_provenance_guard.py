"""Guard tests for scripts/verify_sd_provenance.py (T3).

Proves the isolation check still CATCHES real runtime coupling while NOT
flagging the provenance headers the plan requires (S5). Without this, the
verifier could be silently neutered by a future "make CI green" edit.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/verify_sd_provenance.py"


def _load_verifier(tmp_scan_dir: Path):
    spec = importlib.util.spec_from_file_location("verify_sd_provenance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_sd_provenance"] = module
    spec.loader.exec_module(module)
    module.SCAN_DIRS = [tmp_scan_dir]
    return module


def test_verifier_passes_on_the_real_tree():
    verifier = _load_verifier(REPO_ROOT / "src/maia/servicedesk")
    assert verifier.check_forbidden_references() == []
    assert verifier.check_manifest(verifier.DEFAULT_MANIFEST) == []


def test_verifier_catches_donor_import(tmp_path):
    verifier = _load_verifier(tmp_path)
    (tmp_path / "bad.py").write_text(
        "from agent.memory.context_trim import trim_messages\n", encoding="utf-8"
    )
    problems = verifier.check_forbidden_references()
    assert any("forbidden import" in p for p in problems), problems


def test_verifier_catches_donor_hostname_used_as_value(tmp_path):
    verifier = _load_verifier(tmp_path)
    (tmp_path / "config.py").write_text(
        'RAG_API = "https://smart-document-chatbot.example.com/api"\n',
        encoding="utf-8",
    )
    problems = verifier.check_forbidden_references()
    assert any("runtime reference" in p for p in problems), problems


def test_verifier_allows_provenance_docstrings(tmp_path):
    """The plan REQUIRES documenting the donor in the copied file's docstring."""
    verifier = _load_verifier(tmp_path)
    (tmp_path / "copied.py").write_text(
        '"""Copied from https://github.com/imtarget05/Smart-Document-Chatbot\n'
        "commit babe1c5badcf61f2a3059a5293cb059bdd2c49fd (MIT).\n"
        '"""\n\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    assert verifier.check_forbidden_references() == []


def test_verifier_reports_unparsable_file(tmp_path):
    verifier = _load_verifier(tmp_path)
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    problems = verifier.check_forbidden_references()
    assert any("cannot parse" in p for p in problems), problems


def test_manifest_records_shipped_donor_hash():
    import json

    manifest = json.loads(
        (REPO_ROOT / "docs/service-desk/reuse-manifest.json").read_text(encoding="utf-8")
    )
    entry = next(
        e for e in manifest["entries"] if e["id"] == "smartdoc-context-trim"
    )
    assert entry["status"].startswith("shipped_")
    assert len(entry["donor_sha256"]) == 64
    assert len(entry["sha256"]) == 64
    assert entry["license"].startswith("MIT")


@pytest.mark.parametrize("donor", ["maia", "smart_document_chatbot"])
def test_manifest_donor_commits_are_full_shas(donor):
    import json

    manifest = json.loads(
        (REPO_ROOT / "docs/service-desk/reuse-manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["donors"][donor]["commit"]) == 40
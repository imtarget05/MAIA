"""The abstention gate must FAIL when it should. Until now, nothing asserted that.

THE GAP THIS FILE CLOSES
------------------------
`gate8b_abstention.py` was not referenced by any file under `.github/workflows/`
and no file under `tests/` mentioned `gate8b` at all. A gate that is never run by
a test can be silently neutered -- change `status = "PASS"`, or return early, or
invert a comparison -- and every other signal in the repo stays green, because
nothing was watching. This file is the thing that watches.

It runs the REAL script as a subprocess, against a temporary fixture corpus and
fixture golden set, and asserts on the exit code AND on the emitted JSON. Both
matter: a script that crashes before writing its artifact exits non-zero and
would satisfy an exit-code-only assertion for entirely the wrong reason, so
every negative test here also requires the artifact to exist and to say FAIL.

ISOLATION
---------
The gate reads its corpus, golden set and evidence path from
`MAIA_GATE8B_CORPUS` / `MAIA_GATE8B_GOLDEN` / `MAIA_GATE8B_EVIDENCE` and writes
its BM25 cache to `MAIA_GATE8B_STORAGE`. Pointing all four at `tmp_path` means
the real `eval/golden/*.jsonl` and the shared
`docs/evidence/e2e/gate8b-abstention.json` are never written. That is asserted
by hashing both before and after, not merely asserted in a comment.

OFFLINE DETERMINISM
-------------------
`MAIA_GATE8B_EMBEDDER=offline_test` swaps in a deterministic word n-gram
embedder. The production embedder is a 240MB model download, so a test that
required it would require the network -- and a test that requires the network is
a test that gets skipped exactly when it matters. No test here touches the
network or a live vector store.

A FIXTURE IS NOT A MEASUREMENT
------------------------------
The fixtures are synthetic and are shaped so the two classes are separable by
similarity. That makes the positive control valid; it does not make the real
gate pass, and it does not touch the real corpus, the real
`SIMILARITY_THRESHOLD` or `gate8b_thresholds.yaml`. The real measurement is
reported as it comes out.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
GATE = REPO / "gate8b_abstention.py"
GOLDEN_DIR = REPO / "eval" / "golden"
# The shared evidence artifact lives outside the repo (PROJ/docs/evidence/e2e).
SHARED_EVIDENCE = REPO.parent / "docs" / "evidence" / "e2e" / "gate8b-abstention.json"
PYTHON = sys.executable

# --- fixture data -----------------------------------------------------------
# Eight short, single-chunk policy documents on a narrow invented domain.
CORPUS = {
    "Calibration_Policy.md": (
        "# Alpha Calibration Policy\n\n"
        "Alpha calibration of the widget runs every quarter.\n"
        "The platform team recalibrates the calibration widget.\n"),
    "Annual_Leave_Policy.md": (
        "# Annual Leave Policy\n\n"
        "Employees receive 12 ngay annual leave each calendar year.\n"
        "The manager approves the annual leave request.\n"),
    "Expense_Policy.md": (
        "# Expense Policy\n\n"
        "Taxi and Grab receipts for business trips are reimbursed.\n"
        "Every expense claim requires an invoice.\n"),
    "Security_Policy.md": (
        "# Security Policy\n\n"
        "Passwords are rotated every ninety days.\n"
        "Two factor authentication is mandatory for VPN access.\n"),
    "Device_Policy.md": (
        "# Device Policy\n\n"
        "Laptops are replaced every thirty six months.\n"
        "Each employee receives a keyboard and a headset.\n"),
    "Onboarding_Guide.md": (
        "# Onboarding Guide\n\n"
        "New employees collect a badge and a laptop on day one.\n"
        "A buddy is assigned during the first month.\n"),
    "Travel_Policy.md": (
        "# Travel Policy\n\n"
        "Domestic flights are booked in economy class.\n"
        "Hotels are capped at two million VND per night.\n"),
    "Training_Policy.md": (
        "# Training Policy\n\n"
        "Security training is due at the end of week one.\n"
        "The learning budget is ten million VND per person.\n"),
}

# No-answer probes sit in a domain the corpus does not cover: apiculture,
# aviation, baking, dredging, gliding. Each reuses the word "policy", which
# appears in every document title, so the lexical channel has something to rank
# (B8C3 measures BM25 sensitivity and needs that) while the dense score stays
# well under the 0.3 evidence threshold. That overlap is the "adjacent topic"
# shape the real dataset has.
GOOD_NO_ANSWER = [
    {"id": "FXN-001", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "How many beehives does the rooftop apiary keep under company policy?",
     "probe_terms": ["apiary", "beehive"]},
    {"id": "FXN-002", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "What is the registration fee for a private seaplane licence under company policy?",
     "probe_terms": ["seaplane", "pilotage"]},
    {"id": "FXN-003", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "Which bakery supplies the sourdough starter stipend under company policy?",
     "probe_terms": ["sourdough", "bakery"]},
    {"id": "FXN-004", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "How deep is the municipal harbour channel under company policy?",
     "probe_terms": ["harbour", "dredging"]},
    {"id": "FXN-005", "expect_refusal": True, "reason": "OUT_OF_SCOPE",
     "question": "What is the cruising altitude for the survey glider under company policy?",
     "probe_terms": ["glider", "altitude"]},
]

# Answerable controls restate document sentences closely, which is the whole
# point of a positive control: the gate must authorise a question the corpus
# actually answers.
GOOD_ANSWERABLE = [
    {"id": "FXA-001", "expect_refusal": False,
     "question": "Alpha calibration of the widget runs every quarter"},
    {"id": "FXA-002", "expect_refusal": False,
     "question": "The platform team recalibrates the calibration widget"},
    {"id": "FXA-003", "expect_refusal": False,
     "question": "Employees receive 12 ngay annual leave each calendar year"},
    {"id": "FXA-004", "expect_refusal": False,
     "question": "The manager approves the annual leave request"},
    {"id": "FXA-005", "expect_refusal": False,
     "question": "Passwords are rotated every ninety days"},
    {"id": "FXA-006", "expect_refusal": False,
     "question": "Two factor authentication is mandatory for VPN access"},
    {"id": "FXA-007", "expect_refusal": False,
     "question": "Laptops are replaced every thirty six months"},
    {"id": "FXA-008", "expect_refusal": False,
     "question": "Each employee receives a keyboard and a headset"},
    {"id": "FXA-009", "expect_refusal": False,
     "question": "Domestic flights are booked in economy class"},
    {"id": "FXA-010", "expect_refusal": False,
     "question": "Hotels are capped at two million VND per night"},
    {"id": "FXA-011", "expect_refusal": False,
     "question": "Security training is due at the end of week one"},
    {"id": "FXA-012", "expect_refusal": False,
     "question": "New employees collect a badge and a laptop on day one"},
]

# THE BROKEN DATASET. Every one of these is a question CORPUS answers verbatim,
# labelled expect_refusal=true. B8A1 (schema) still passes -- the rows are
# well-formed -- so the thing that has to catch this is the answerability oracle
# reading the corpus, plus the routing check.
BAD_NO_ANSWER = [
    {"id": "BAD-001", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "Alpha calibration of the widget runs every quarter",
     "probe_terms": ["calibration widget", "every quarter"]},
    {"id": "BAD-002", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "Employees receive 12 ngay annual leave each calendar year",
     "probe_terms": ["12 ngay", "annual leave"]},
    {"id": "BAD-003", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "Laptops are replaced every thirty six months",
     "probe_terms": ["thirty six months"]},
    {"id": "BAD-004", "expect_refusal": True, "reason": "NO_SUPPORTING_DOCUMENT",
     "question": "Passwords are rotated every ninety days",
     "probe_terms": ["ninety days"]},
]


# --- harness ----------------------------------------------------------------

def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")


def _build_tree(root: pathlib.Path, no_answer: list[dict],
                answerable: list[dict] | None = None) -> pathlib.Path:
    (root / "corpus").mkdir(parents=True)
    (root / "golden").mkdir(parents=True)
    for name, body in CORPUS.items():
        (root / "corpus" / name).write_text(body, encoding="utf-8")
    _write_jsonl(root / "golden" / "no_answer.jsonl", no_answer)
    _write_jsonl(root / "golden" / "answerable.jsonl",
                 answerable if answerable is not None else GOOD_ANSWERABLE)
    return root


def _run_gate(root: pathlib.Path) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Run the real gate against `root`. Returns (proc, payload-or-None)."""
    evidence = root / "evidence.json"
    env = {
        # Deliberately minimal: no cloud credentials, so the gate cannot reach a
        # hosted embedding provider even by accident.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "MAIA_GATE8B_CORPUS": str(root / "corpus"),
        "MAIA_GATE8B_GOLDEN": str(root / "golden"),
        "MAIA_GATE8B_EVIDENCE": str(evidence),
        "MAIA_GATE8B_STORAGE": str(root / "storage"),
        "MAIA_GATE8B_EMBEDDER": "offline_test",
    }
    proc = subprocess.run([PYTHON, str(GATE)], capture_output=True, text=True,
                          env=env, cwd=str(REPO), timeout=600)
    payload = json.loads(evidence.read_text()) if evidence.exists() else None
    return proc, payload


def _golden_digest() -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(GOLDEN_DIR.glob("*.jsonl"))}


def _shared_evidence_digest() -> str | None:
    if not SHARED_EVIDENCE.exists():
        return None
    return hashlib.sha256(SHARED_EVIDENCE.read_bytes()).hexdigest()


# --- THE NEGATIVE CONTROL ---------------------------------------------------

def test_gate_fails_when_every_no_answer_row_is_actually_answerable(tmp_path):
    """The regression this file exists for.

    Four rows claim the corpus cannot answer them; the corpus answers all four
    verbatim. The gate must notice. `force status = "PASS"` makes this fail.
    """
    root = _build_tree(tmp_path / "bad", BAD_NO_ANSWER)
    proc, payload = _run_gate(root)

    assert payload is not None, (
        "the gate wrote no evidence artifact, so there is no verdict to judge; "
        f"an exit code alone proves nothing here. stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}")
    assert payload["status"] == "FAIL", (
        f"expected FAIL on a dataset whose no-answer rows are answerable, got "
        f"{payload['status']!r}; blocking={payload['blocking_failures']}")
    assert proc.returncode != 0, (
        "a FAIL verdict must exit non-zero, otherwise every caller that checks "
        "only the exit code sees success")


def test_the_answerability_oracle_is_what_catches_the_bad_dataset(tmp_path):
    """Not just 'something failed' -- the right check failed.

    Separated from the test above on purpose. A gate can fail for the wrong
    reason (a crash, an import error) and still satisfy a bare status assertion.
    B8A2 is the check whose entire job is to search the corpus and contradict a
    label, so it is the one that must be the witness.
    """
    root = _build_tree(tmp_path / "bad2", BAD_NO_ANSWER)
    _, payload = _run_gate(root)
    assert payload is not None
    assert "B8A2" in payload["blocking_failures"], (
        f"B8A2 (answerability oracle) should have flagged the mislabelled rows; "
        f"blocking failures were {payload['blocking_failures']}")
    b8a2 = next(c for c in payload["checks"] if c["id"] == "B8A2")
    assert b8a2["status"] == "INVALID_GOLDEN_ROW"
    # Every bad row's probe term is in the corpus, so all four must be named.
    assert "MISLABELLED" in b8a2["detail"]


def test_gate_reports_the_classes_as_not_separable_on_the_bad_dataset(tmp_path):
    """The separability verdict must also be a FAIL, not a quiet True.

    This is the specific measured defect the task is about: with no-answer rows
    sitting in the answerable score band, no threshold can separate the classes.
    """
    root = _build_tree(tmp_path / "bad3", BAD_NO_ANSWER)
    _, payload = _run_gate(root)
    assert payload is not None
    assert payload["abstention"]["separable"] is False
    assert payload["conclusion"]["separable_by_similarity"] is False
    assert "B8B3" in payload["blocking_failures"]


def test_an_empty_no_answer_set_is_never_a_pass(tmp_path):
    """Zero denominator is INVALID_DATASET, not 0/0 = PASS.

    This is the false-pass shape that makes a meaningless abstention rate look
    like a perfect one.
    """
    root = _build_tree(tmp_path / "empty", [])
    proc, payload = _run_gate(root)
    assert payload is not None
    assert payload["status"] != "PASS", (
        "an empty no-answer set must not be scored as a pass")
    assert proc.returncode != 0
    b8b4 = next(c for c in payload["checks"] if c["id"] == "B8B4")
    assert b8b4["status"] == "INVALID_DATASET"


# --- THE POSITIVE CONTROL ---------------------------------------------------
# Without this, the negative tests above are satisfiable by a gate that always
# fails. That is the failure mode this pair exists to rule out.

def test_gate_passes_on_a_known_good_separable_fixture(tmp_path):
    root = _build_tree(tmp_path / "good", GOOD_NO_ANSWER)
    proc, payload = _run_gate(root)

    assert payload is not None, f"no artifact written. stdout:\n{proc.stdout}"
    assert payload["blocking_failures"] == [], (
        f"every check should pass on a separable fixture; failing: "
        f"{payload['blocking_failures']}")
    assert payload["status"] == "PASS", payload["checks"]
    assert proc.returncode == 0, "a PASS verdict must exit zero"


def test_the_good_fixture_actually_exercises_both_classes(tmp_path):
    """Guards the positive control against being vacuous.

    If the fixture stopped containing answerable questions, `rate=0.0` would
    also "pass" a threshold check -- the exact shape of a broken retriever.
    B8B2 exists to catch that, so assert both poles are populated.
    """
    root = _build_tree(tmp_path / "good2", GOOD_NO_ANSWER)
    _, payload = _run_gate(root)
    assert payload is not None
    a = payload["abstention"]
    assert a["rows"] > 0 and a["abstained"] == a["rows"]
    assert a["answerable_controls"] > 0
    assert a["answerable_authorized"] == a["answerable_controls"]
    assert a["separable"] is True


# --- ISOLATION --------------------------------------------------------------

def test_a_fixture_run_leaves_the_real_golden_data_byte_identical(tmp_path):
    before = _golden_digest()
    shared_before = _shared_evidence_digest()
    assert before, "no golden files found; the assertion below would be vacuous"

    root = _build_tree(tmp_path / "iso", BAD_NO_ANSWER)
    proc, payload = _run_gate(root)
    assert payload is not None, proc.stderr

    after = _golden_digest()
    assert after == before, (
        "the gate run mutated the real golden data: "
        f"{ {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k)} }")
    assert _shared_evidence_digest() == shared_before, (
        "the fixture run overwrote the shared evidence artifact "
        f"{SHARED_EVIDENCE}; MAIA_GATE8B_EVIDENCE is not being honoured")


def test_the_gate_writes_only_inside_the_fixture_tree(tmp_path):
    """No stray files in the repo root from a fixture run.

    HybridRetriever caches its BM25 corpus to disk; without
    MAIA_GATE8B_STORAGE a fixture run writes storage/ into the repo.
    """
    root = _build_tree(tmp_path / "stray", BAD_NO_ANSWER)
    _, payload = _run_gate(root)
    assert payload is not None
    storage = root / "storage"
    assert storage.exists(), (
        "expected the BM25 cache inside the fixture tree; the gate is probably "
        "writing to ./storage in the repo instead")


# --- ANTI-CONFUSION ---------------------------------------------------------

def test_a_non_production_run_is_labelled_as_one_in_the_artifact(tmp_path):
    """A fixture measurement must never be quotable as the real one.

    The offline embedder exists so these tests need no network. If its numbers
    could be mistaken for production numbers, the honest alternative would be to
    have no test at all -- so the artifact carries the flag on its face.
    """
    root = _build_tree(tmp_path / "label", GOOD_NO_ANSWER)
    _, payload = _run_gate(root)
    assert payload is not None
    assert payload["production_embedder"] is False
    assert payload["real_dataset"] is False
    assert "MAIA_GATE8B_EMBEDDER" in payload["overrides_active"]
    assert payload["embedder_mode"].startswith("offline_test")


def test_the_conclusion_states_the_defect_with_its_denominator(tmp_path):
    """Disclosure shape, asserted on a real artifact.

    Locks in the properties that make the verdict quotable: the abstention rate
    carries n, the separability claim carries BOTH measured numbers, and the
    artifact says in words that neither a threshold nor a label was adjusted to
    reach a verdict.
    """
    root = _build_tree(tmp_path / "concl", BAD_NO_ANSWER)
    _, payload = _run_gate(root)
    assert payload is not None
    c = payload["conclusion"]

    assert f"n={payload['abstention']['rows']}" in c["abstention"]
    assert c["separable_by_similarity"] is False
    a = payload["abstention"]
    assert f"{a['max_noanswer_top_dense']:.4f}" in c["separability_evidence"]
    assert f"{a['min_answerable_top_dense']:.4f}" in c["separability_evidence"]
    assert "NOT reached by tuning a threshold" in c["why_not_a_threshold_change"]
    assert "No golden row was relabelled" in c["why_not_a_data_change"]
    assert "DIFFERENT DECISION SIGNAL" in c["consequence"]
    assert "proto_answerability_signals.py" in c["cross_reference"]


def test_no_threshold_override_knob_exists_in_the_gate():
    """The gate must not be able to be talked into a different threshold.

    A MAIA_GATE8B_THRESHOLD would be exactly the knob this whole exercise exists
    to refuse, so its absence is asserted rather than assumed.
    """
    source = GATE.read_text(encoding="utf-8")
    for forbidden in ("MAIA_GATE8B_THRESHOLD", "MAIA_GATE8B_MIN_ANSWERABLE",
                      "MAIA_GATE8B_SIMILARITY"):
        assert forbidden not in source, (
            f"{forbidden} would let a caller move the gate's decision boundary")
    # The one threshold that does exist must still be read from the YAML file.
    assert "min_answerable_authorized" in source
    assert 'TH = CFG.get("ABSTENTION", {})' in source


@pytest.mark.parametrize("golden", sorted(p.name for p in GOLDEN_DIR.glob("*.jsonl")))
def test_the_real_gate_script_remains_a_script_not_an_importable_module(golden):
    """Guard against the verdict moving to import time.

    If the gate became an importable module that computed its verdict on import,
    the subprocess tests above could pass while the CLI path regressed.
    """
    assert GATE.exists()
    assert "sys.exit(" in GATE.read_text(encoding="utf-8")

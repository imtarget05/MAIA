#!/usr/bin/env python3
"""Verify Service Desk reuse provenance (plan T0).

Checks, fail-fast and CI-friendly:
1. ``docs/service-desk/reuse-manifest.json`` parses and every ``copy`` /
   ``copy_adapt`` entry has a destination file that exists (or is still
   ``planned_*``).
2. For shipped entries (status not ``planned_*``), the recorded ``sha256``
   matches the destination file — proving the audited copy is what shipped.
3. No file under ``src/maia/servicedesk/`` contains forbidden references
   to project 1 (isolation constraint, Global Constraints S4/S5).

Usage:  python scripts/verify_sd_provenance.py [--manifest PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "docs/service-desk/reuse-manifest.json"
SCAN_DIRS = [REPO_ROOT / "src/maia/servicedesk"]

FORBIDDEN_SUBSTRINGS = (
    "smart-document-chatbot",
    "llm-router",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_manifest(manifest_path: Path) -> list[str]:
    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest unreadable: {manifest_path}: {exc}"]

    donors = manifest.get("donors", {})
    if "maia" not in donors:
        problems.append("manifest must record the MAIA donor commit")
    for name, donor in donors.items():
        if len(donor.get("commit", "")) != 40:
            problems.append(f"donor '{name}' commit must be a full 40-char SHA")

    for entry in manifest.get("entries", []):
        entry_id = entry.get("id", "<missing-id>")
        kind = entry.get("kind", "")
        status = entry.get("status", "")
        if kind in ("copy", "copy_adapt") and not status.startswith("planned_"):
            dest = REPO_ROOT / entry.get("destination_path", "")
            if not dest.exists():
                problems.append(f"[{entry_id}] shipped copy missing: {dest}")
            elif not entry.get("sha256"):
                problems.append(f"[{entry_id}] shipped copy lacks sha256")
            elif sha256_of(dest) != entry["sha256"]:
                problems.append(
                    f"[{entry_id}] sha256 mismatch for {dest} "
                    "(shipped file differs from audited donor copy)"
                )
    return problems


def check_forbidden_references() -> list[str]:
    problems: list[str] = []
    for scan_dir in SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for py_file in scan_dir.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="replace")
            lowered = text.lower()
            for needle in FORBIDDEN_SUBSTRINGS:
                if needle.lower() in lowered:
                    problems.append(
                        f"forbidden reference '{needle}' in {py_file.relative_to(REPO_ROOT)}"
                    )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    problems = check_manifest(args.manifest) + check_forbidden_references()
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        print(f"{len(problems)} provenance problem(s) found", file=sys.stderr)
        return 1
    print("provenance OK: manifest valid, no forbidden references")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

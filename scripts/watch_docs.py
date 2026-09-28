"""Polling file-watcher for MAIA doc dirs (stdlib only).

Scans the given document directories for new or modified files by comparing
``mtime + size`` against a JSON state file, and prints one machine-readable
trigger line per change::

    INGEST <absolute-path>

Design notes (current architecture, kept deliberately):

* This watcher invokes NOTHING external. It only emits the trigger list.
  Ingestion itself stays manual via the existing CLI::

      python -m maia.cli ingest

  A scheduler (cron / systemd timer / CI job) can consume the ``INGEST``
  lines and decide when to run the ingest command.

* State file format: JSON ``{absolute_path: {"size": int, "mtime_ns": int}}``.
  Deleted files are dropped from the state silently (no trigger line).

* Modes:
    --once            single scan, then exit 0 (for CI).
    --poll SECS       loop: scan, sleep SECS, repeat until Ctrl-C (exit 0).
  If both are given, ``--once`` wins (single scan).

Usage::

    python scripts/watch_docs.py data/samples data/enterprise --once
    python scripts/watch_docs.py data/samples --poll 30 --state storage/.watch_state.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRS = [str(REPO_ROOT / "data" / "samples"), str(REPO_ROOT / "data" / "enterprise")]
DEFAULT_STATE = str(REPO_ROOT / ".watch_docs_state.json")


def _snapshot(paths: list[Path]) -> dict[str, dict[str, int]]:
    """Return {absolute_path: {"size":, "mtime_ns":}} for existing files."""
    snap: dict[str, dict[str, int]] = {}
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file():
            continue
        snap[str(p)] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return snap


def collect_files(dirs: list[str]) -> list[Path]:
    """Recursively collect regular files under *dirs* (sorted, resolved)."""
    found: list[Path] = []
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                found.append(p.resolve())
            elif p.is_symlink():
                try:
                    target = p.resolve()
                except OSError:
                    continue
                if target.is_file():
                    found.append(target)
    # De-duplicate (symlink + real path pointing at the same file).
    seen: dict[str, Path] = {}
    for p in found:
        seen.setdefault(str(p), p)
    return [seen[k] for k in sorted(seen)]


def load_state(state_file: str) -> dict[str, dict[str, int]]:
    try:
        with open(state_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_state(state_file: str, state: dict[str, dict[str, int]]) -> None:
    path = Path(state_file)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    tmp.replace(path)


def scan(
    dirs: list[str],
    state_file: str,
    *,
    out=None,
) -> list[str]:
    """Single scan: print ``INGEST <path>`` per new/modified file.

    Returns the list of changed absolute paths. Persists the new snapshot
    to *state_file* so state survives across runs. Never touches the real
    ingest pipeline.
    """
    if out is None:
        out = sys.stdout
    state_path = str(Path(state_file).resolve())
    previous = load_state(state_path)
    files = [p for p in collect_files(dirs) if str(p) != state_path]
    current = _snapshot(files)
    changed = sorted(
        p for p, meta in current.items() if previous.get(p) != meta
    )
    for p in changed:
        out.write(f"INGEST {p}\n")
    out.flush()
    save_state(state_path, current)
    return changed


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Poll doc dirs and print INGEST <path> for new/modified files."
    )
    ap.add_argument("dirs", nargs="*", default=DEFAULT_DIRS,
                    help="Doc directories to watch (recursive).")
    ap.add_argument("--state", default=DEFAULT_STATE,
                    help="JSON state file (mtime+size snapshot).")
    ap.add_argument("--once", action="store_true",
                    help="Single scan, then exit 0 (for CI).")
    ap.add_argument("--poll", type=float, default=0.0, metavar="SECS",
                    help="Loop: rescan every SECS seconds until Ctrl-C.")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once or args.poll <= 0:
        scan(args.dirs, args.state)
        return 0
    try:
        while True:
            scan(args.dirs, args.state)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

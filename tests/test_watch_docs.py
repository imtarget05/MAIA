"""Tests for scripts/watch_docs.py (stdlib + pytest only).

Never touches real doc dirs: everything runs under tmp_path, and the module
is loaded by file path (scripts/ has no package import) so no heavy
dependency is ever imported.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCH_DOCS = REPO_ROOT / "scripts" / "watch_docs.py"


def _load_watch_docs():
    spec = importlib.util.spec_from_file_location("watch_docs", WATCH_DOCS)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_wd = _load_watch_docs()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_once_new_file_detected_and_exits_zero(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "a.md", "hello")
    state = str(tmp_path / "state.json")
    assert _wd.main([str(docs), "--once", "--state", state]) == 0
    saved = json.loads(Path(state).read_text(encoding="utf-8"))
    assert len(saved) == 1


def test_once_modified_file_detected(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "a.md", "v1")
    state = str(tmp_path / "state.json")
    assert _wd.main([str(docs), "--once", "--state", state]) == 0
    target = docs / "a.md"
    # Force a different size AND mtime (some filesystems have coarse mtime).
    _write(target, "v1 plus more content")
    import os

    st = os.stat(target)
    os.utime(target, (st.st_atime + 5, st.st_mtime + 5))
    buf = io.StringIO()
    changed = _wd.scan([str(docs)], state, out=buf)
    assert changed == [str(target.resolve())]
    assert buf.getvalue().strip() == f"INGEST {target.resolve()}"


def test_once_unchanged_is_silent(tmp_path, capsys):
    docs = tmp_path / "docs"
    _write(docs / "a.md", "hello")
    state = str(tmp_path / "state.json")
    assert _wd.main([str(docs), "--once", "--state", state]) == 0
    capsys.readouterr()  # drain first-run output
    assert _wd.main([str(docs), "--once", "--state", state]) == 0
    assert capsys.readouterr().out == ""


def test_state_persists_across_runs(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "a.md", "hello")
    state = str(tmp_path / "state.json")
    assert _wd.main([str(docs), "--once", "--state", state]) == 0
    first = json.loads(Path(state).read_text(encoding="utf-8"))
    _write(docs / "b.md", "world")
    buf = io.StringIO()
    changed = _wd.scan([str(docs)], state, out=buf)
    assert len(changed) == 1 and changed[0].endswith("b.md")
    second = json.loads(Path(state).read_text(encoding="utf-8"))
    assert set(second) == set(first) | {changed[0]}
    # Third run with no changes: silent, state identical.
    buf2 = io.StringIO()
    assert _wd.scan([str(docs)], state, out=buf2) == []
    assert buf2.getvalue() == ""
    assert json.loads(Path(state).read_text(encoding="utf-8")) == second


def test_ingest_line_format_and_sorted_order(tmp_path):
    docs = tmp_path / "docs"
    for name in ("c.md", "a.md", "b.md"):
        _write(docs / name, name)
    buf = io.StringIO()
    changed = _wd.scan([str(docs)], str(tmp_path / "s.json"), out=buf)
    lines = buf.getvalue().splitlines()
    assert lines == [f"INGEST {p}" for p in changed] == sorted(lines)
    assert all(line.startswith("INGEST ") for line in lines)


def test_deleted_file_dropped_silently(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "a.md", "hello")
    _write(docs / "gone.md", "bye")
    state = str(tmp_path / "state.json")
    _wd.scan([str(docs)], state, out=io.StringIO())
    (docs / "gone.md").unlink()
    buf = io.StringIO()
    assert _wd.scan([str(docs)], state, out=buf) == []
    assert buf.getvalue() == ""
    saved = json.loads(Path(state).read_text(encoding="utf-8"))
    assert all("gone.md" not in k for k in saved)


def test_missing_dir_is_not_an_error(tmp_path):
    buf = io.StringIO()
    assert _wd.scan([str(tmp_path / "nope")], str(tmp_path / "s.json"), out=buf) == []
    assert _wd.main([str(tmp_path / "nope"), "--once", "--state", str(tmp_path / "s.json")]) == 0


def test_watcher_invokes_nothing_external(tmp_path):
    src = WATCH_DOCS.read_text(encoding="utf-8")
    for banned in ("subprocess", "os.system", "os.exec", "os.spawn", "ingest_data_dir"):
        assert banned not in src

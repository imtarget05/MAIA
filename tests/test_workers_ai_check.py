"""Workers AI connectivity check script (scripts/check_workers_ai.py).

TDD slice: Cloudflare Workers AI config (KHÔNG R2).
- Mock khi thieu creds (khong hardcode secret, khong goi mang mac dinh).
- Bao cao llm_mode / embed_mode tu settings hien tai.
"""
import os
import runpy
import sys

import pytest

SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "check_workers_ai.py"
)


def _run_script(monkeypatch, *argv):
    # NOTE: maia.config.settings la singleton, duoc khoi tao luc import
    # (collection cua file test khac co the import truoc) — phai patch
    # truc tiep attribute, khong chi env var.
    from maia.config import settings as _s

    monkeypatch.setattr(sys, "argv", ["check_workers_ai.py", *argv])
    monkeypatch.setattr(_s, "CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setattr(_s, "CLOUDFLARE_API_TOKEN", "")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setenv("MAIA_MODE", "mock")
    monkeypatch.setenv("MAIA_EMBED_FORCE_HASH", "1")
    return runpy.run_path(os.path.realpath(SCRIPT), run_name="__main__")


def test_script_exists():
    assert os.path.isfile(os.path.realpath(SCRIPT)), "scripts/check_workers_ai.py missing"


def test_mock_mode_without_creds_exits_zero(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_script(monkeypatch)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "mock" in out.lower()


def test_script_contains_no_hardcoded_secret():
    import re

    with open(os.path.realpath(SCRIPT), encoding="utf-8") as f:
        src = f.read()
    for prefix in ("cfat_", "cfut_", "GOCSPX-", "sk-"):
        assert prefix not in src, f"possible hardcoded secret prefix {prefix!r}"
    # Khong hardcode account-id/token that dang chuoi hex 32 ky tu
    assert not re.search(r"\b[0-9a-f]{32}\b", src), "possible hardcoded account id/token"


def test_no_network_by_default(monkeypatch):
    """Mac dinh khong goi mang: requests.post phai KHONG duoc goi."""
    import requests

    called = []
    orig_post = requests.post
    orig_session_post = requests.Session.post

    def _boom(*a, **k):
        called.append(True)
        raise AssertionError("network call in default (non-live) mode")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(requests.Session, "post", _boom)
    try:
        with pytest.raises(SystemExit) as exc:
            _run_script(monkeypatch)
    finally:
        monkeypatch.setattr(requests, "post", orig_post)
        monkeypatch.setattr(requests.Session, "post", orig_session_post)
    assert exc.value.code == 0
    assert not called


def test_fake_creds_report_cloudflare_without_network(monkeypatch, capsys):
    """Code doc dung ACCOUNT_ID + TOKEN: creds gia -> cloudflare mode, van khong goi mang."""
    import requests

    from maia.config import settings as _s

    monkeypatch.setattr(sys, "argv", ["check_workers_ai.py"])
    monkeypatch.setattr(_s, "CLOUDFLARE_ACCOUNT_ID", "acc")
    monkeypatch.setattr(_s, "CLOUDFLARE_API_TOKEN", "tok")

    def _boom(*a, **k):
        raise AssertionError("network call in default (non-live) mode")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(requests.Session, "post", _boom)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(os.path.realpath(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "cloudflare" in out.lower()

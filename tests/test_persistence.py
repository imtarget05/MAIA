"""
Test durable persistence — Phase 8.

Hai mức kiểm, tách bạch vì chúng bắt lỗi khác nhau:

    * Tầng config (luôn chạy, không cần DB): không có DSN thì phải nói rõ,
      không âm thầm dùng DSN mặc định.
    * Tầng runtime (cần Postgres): state sống qua process restart thật.

Bằng chứng process-restart thật nằm ở
`docs/_p8_process_restart_probe.py`, vì một unit test không thể tự giết
chính nó rồi chứng minh điều gì đó.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

# tests/ -> MAIA-clean/ -> Projects/
MAIA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(MAIA_ROOT, "src")
PROJECTS = os.path.dirname(MAIA_ROOT)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from maia.persistence import (  # noqa: E402
    ALL_TIERS,
    DEFAULT_CHECKPOINT_TABLE,
    ENV_DSN,
    PersistenceUnavailable,
    checkpoint_table_from_env,
    durable_checkpointer,
    dsn_from_env,
    postgres_available,
)

DSN = os.environ.get(ENV_DSN)
needs_db = pytest.mark.skipif(
    not DSN, reason=f"can {ENV_DSN} troi toi Postgres de test runtime")


# ==========================================================================
# Tang config — luon chay
# ==========================================================================
def test_all_three_tiers_are_distinct():
    """Ba tang persistence phai la ba thu khac nhau, khong gop lam mot."""
    assert set(ALL_TIERS) == {"thread_state", "long_term_memory", "rag_index"}
    assert len(set(ALL_TIERS)) == 3


def test_dsn_has_no_hardcoded_default(monkeypatch):
    """
    Khong DSN thi phai raise, khong duoc fallback ve mot DSN nao do.

    Mot DSN mac dinh trong code nghia la credential nam trong repo va moi truong
    khac se am tham ghi vao dung database do.
    """
    monkeypatch.delenv(ENV_DSN, raising=False)
    with pytest.raises(PersistenceUnavailable) as exc:
        dsn_from_env()
    assert ENV_DSN in str(exc.value), "thong bao phai chi ro can bien moi truong nao"


def test_dsn_reads_from_env(monkeypatch):
    monkeypatch.setenv(ENV_DSN, "postgresql://u:p@h:5432/d")
    assert dsn_from_env() == "postgresql://u:p@h:5432/d"
    assert postgres_available() is True


def test_postgres_available_is_false_without_dsn(monkeypatch):
    monkeypatch.delenv(ENV_DSN, raising=False)
    assert postgres_available() is False


def test_checkpoint_table_default_and_override(monkeypatch):
    monkeypatch.delenv("MAIA_CHECKPOINT_TABLE", raising=False)
    assert checkpoint_table_from_env() == DEFAULT_CHECKPOINT_TABLE
    monkeypatch.setenv("MAIA_CHECKPOINT_TABLE", "custom_ckpt")
    assert checkpoint_table_from_env() == "custom_ckpt"


def test_no_credential_literal_in_source():
    """
    Source khong duoc chua DSN co credential that.

    Chi kiem DSN *co user:password*, khong kiem moi chuoi `postgresql://` —
    vi du trong thong bao loi la hop le va khong phai secret.
    """
    import re
    path = os.path.join(SRC, "maia", "persistence.py")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # Chỉ flag DSN có user:pass ở dạng lowercase-thực-tế. Placeholder viết HOA
    # (USER:PASS) là tài liệu, không phải secret.
    literals = re.findall(r"postgresql://[^\s'\"`]+", text)
    with_creds = [
        d for d in literals
        if re.search(r"://[a-z0-9_.-]+:[a-z0-9_.-]+@", d)
        and "@" in d
    ]
    assert not with_creds, f"khong hardcode DSN co credential that: {with_creds}"
    assert "password" not in text.lower(), "tu khoa password trong source"


# ==========================================================================
# Tang runtime — can Postgres
# ==========================================================================
@needs_db
def test_durable_checkpointer_setup_creates_schema():
    async def run():
        async with durable_checkpointer() as saver:
            return saver is not None
    assert asyncio.run(run()) is True


@needs_db
def test_state_persists_across_two_sequential_processes():
    """
    Chay hai process that: tien trinh dau ghi state roi chet, tien trinh sau
    doc lai. Day la kiem chung toi thieu cho tinh ben vung.
    """
    probe = os.path.join(PROJECTS, "docs", "_p8_process_restart_probe.py")
    if not os.path.isfile(probe):
        pytest.skip("probe script khong ton tai")

    env = {**os.environ, ENV_DSN: DSN}
    w = subprocess.run([sys.executable, probe, "write"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert w.returncode == 0, f"write phase that: {w.stderr[-500:]}"

    r = subprocess.run([sys.executable, probe, "read"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, f"read phase that: {r.stderr[-500:]}"
    assert "RESULT: PASS" in r.stdout
    assert '"processes_differ": true' in r.stdout
    assert '"state_survived_restart": true' in r.stdout
    assert '"resumed_correctly": true' in r.stdout

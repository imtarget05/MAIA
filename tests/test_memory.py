"""Tests for long-term memory (SQLite, offline, hermetic :memory: DB)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia.agent.memory import LongTermMemory, extract_memories, ltm_context, ltm_learn
from maia.config import settings


def _mem():
    return LongTermMemory(":memory:")


def test_extract_preferences():
    assert extract_memories("Tôi thích trả lời bằng tiếng Việt")[0][0] == "preference"
    assert extract_memories("Hãy gọi tôi là An")[0][0] == "fact"
    assert extract_memories("Chính sách nghỉ phép thế nào?") == []


def test_store_recall_roundtrip():
    m = _mem()
    mid = m.store("u1", "tA", "preference", "Tôi thích trả lời bằng tiếng Việt")
    assert mid is not None
    hits = m.recall("u1", "tA", "trả lời tiếng Việt giúp tôi")
    assert hits and hits[0]["content"] == "Tôi thích trả lời bằng tiếng Việt"


def test_tenant_isolation():
    m = _mem()
    m.store("u1", "tA", "preference", "Tôi thích tiếng Việt")
    assert m.recall("u1", "tB", "tiếng Việt") == []
    assert m.recall("u2", "tA", "tiếng Việt") == []


def test_recall_no_overlap_empty():
    m = _mem()
    m.store("u1", "tA", "fact", "Tên tôi là An")
    assert m.recall("u1", "tA", "chính sách nghỉ phép") == []


def test_forget_and_clear():
    m = _mem()
    mid = m.store("u1", "tA", "fact", "Tên tôi là An")
    assert m.count("u1", "tA") == 1
    assert m.forget("u1", "tA", mid) is True
    assert m.count("u1", "tA") == 0
    m.store("u1", "tA", "fact", "Tên tôi là An")
    assert m.clear_user("u1", "tA") == 1


def test_dedupe_same_content():
    m = _mem()
    a = m.store("u1", "tA", "preference", "Tôi thích tiếng Việt")
    b = m.store("u1", "tA", "preference", "Tôi thích tiếng Việt")
    assert a == b
    assert m.count("u1", "tA") == 1


def test_helpers_noop_when_disabled():
    old = settings.LTM_ENABLED
    settings.LTM_ENABLED = False
    try:
        assert ltm_context("u1", "tA", "anything") == ""
        assert ltm_learn("u1", "tA", "Tôi thích tiếng Việt") == []
    finally:
        settings.LTM_ENABLED = old

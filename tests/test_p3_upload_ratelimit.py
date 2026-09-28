"""P3: magic-byte upload validation + Redis-limiter fallback."""
import pytest

from maia import api as api_mod
from maia.ingestion import validate_upload_bytes


def test_pdf_requires_header():
    assert validate_upload_bytes(b"%PDF-1.4 fake", ".pdf") is None
    assert validate_upload_bytes(b"hello", ".pdf") is not None


def test_docx_requires_zip_header():
    assert validate_upload_bytes(b"PK\x03\x04 fake", ".docx") is None
    assert validate_upload_bytes(b"hello", ".docx") is not None


def test_text_rejects_binary():
    assert validate_upload_bytes(b"plain text ok", ".md") is None
    assert validate_upload_bytes(b"ab\x00cd", ".txt") is not None
    assert validate_upload_bytes(b"", ".csv") is not None


def test_limiter_falls_back_when_redis_unreachable(monkeypatch):
    api_mod._rl_hits.clear()
    monkeypatch.setattr(api_mod.settings, "REDIS_URL", "redis://127.0.0.1:6399")
    monkeypatch.setattr(api_mod.settings, "CHAT_RATE_LIMIT_PER_MIN", 2)
    api_mod.check_chat_rate_limit("fallback-user")
    api_mod.check_chat_rate_limit("fallback-user")
    with pytest.raises(Exception) as e:
        api_mod.check_chat_rate_limit("fallback-user")
    assert getattr(e.value, "status_code", None) == 429
    monkeypatch.setattr(api_mod.settings, "CHAT_RATE_LIMIT_PER_MIN", 60)

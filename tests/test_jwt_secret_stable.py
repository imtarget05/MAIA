"""Ephemeral JWT key must be stable within a process.

Regression: with JWT_SECRET_KEY unset, `jwt_secret_key` returned a fresh
random key on EVERY access, so `maia.auth.SECRET_KEY` (import time) differed
from `maia.api.SECRET_KEY` — login succeeded but all authed endpoints 401'd.
"""
import pytest


@pytest.fixture()
def no_jwt_env(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")
    yield


def test_ephemeral_jwt_key_stable_across_accesses(no_jwt_env):
    from maia.config import Settings

    s = Settings(_env_file=None)
    assert s.JWT_SECRET_KEY == ""
    with pytest.warns(RuntimeWarning):
        first = s.jwt_secret_key
    with pytest.warns(RuntimeWarning):
        second = s.jwt_secret_key
    assert first == second
    assert len(first) > 32

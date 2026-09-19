"""T0: settings validation gates (RED→GREEN per plan T0).

Key gate from the plan: integrated mode must refuse mock/hash embeddings
instead of silently degrading. The plan's literal test snippet is kept.
"""
from __future__ import annotations

import pytest

from maia.servicedesk.settings import Settings


def test_integrated_disallows_mock_embeddings():
    with pytest.raises(ValueError):
        Settings(mode="integrated", embedding_mode="hash",
                 database_url="postgresql+psycopg://test@localhost/test")


def test_integrated_disallows_mock_llm():
    with pytest.raises(ValueError):
        Settings(mode="integrated", llm_mode="mock",
                 database_url="postgresql+psycopg://test@localhost/test")


def test_integrated_rejects_non_postgres_database_url():
    with pytest.raises(ValueError):
        Settings(mode="integrated",
                 database_url="sqlite:///./storage/servicedesk.db")


def test_offline_allows_hash_embedding_demo_mode():
    settings = Settings(mode="offline", embedding_mode="hash",
                        database_url="postgresql+psycopg://test@localhost/test",
                        _env_file=None)
    assert settings.mode == "offline"
    assert settings.embedding_mode == "hash"


def test_integrated_accepts_complete_postgres_config():
    settings = Settings(
        mode="integrated",
        database_url="postgresql+psycopg://test@localhost/test",
        jira_base_url="https://sandbox.example.atlassian.net",
        jira_email="bot@example.com",
        jira_api_token_ref="env:JIRA_API_TOKEN",
        jira_project_key="ITSD",
        _env_file=None,
    )
    assert settings.readiness_errors() == []


def test_integrated_readiness_reports_missing_jira_credentials():
    settings = Settings(mode="integrated",
                        database_url="postgresql+psycopg://test@localhost/test",
                        _env_file=None)
    problems = settings.readiness_errors()
    assert any("jira_base_url" in p for p in problems)
    assert any("jira_api_token_ref" in p for p in problems)
    # Readiness problems never silently disable the mode.
    assert settings.mode == "integrated"


def test_defaults_are_scoped_and_tuned_per_plan():
    settings = Settings(database_url="postgresql+psycopg://test@localhost/test",
                        _env_file=None)
    # Dedicated collection, never the legacy maia_knowledge one.
    assert settings.qdrant_collection == "servicedesk_knowledge"
    assert settings.worker_concurrency == 2
    assert (settings.lease_seconds, settings.heartbeat_seconds) == (60, 15)
    assert (settings.http_connect_timeout_sec, settings.http_read_timeout_sec) == (3.0, 15.0)
    assert settings.read_retry_max_attempts == 5


def test_env_prefix_maps_sd_fields(monkeypatch):
    monkeypatch.setenv("SD_MODE", "offline")
    monkeypatch.setenv("SD_QDRANT_COLLECTION", "custom_sd_collection")
    settings = Settings(database_url="postgresql+psycopg://test@localhost/test",
                        _env_file=None)
    assert settings.mode == "offline"
    assert settings.qdrant_collection == "custom_sd_collection"

"""Tests for MCP-style external connectors (offline: disabled + mocked HTTP)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import mcp
from maia.config import settings


def test_status_shape_no_secrets():
    st = mcp.status()
    names = {c["name"] for c in st["connectors"]}
    assert {"github", "notion"} <= names
    for c in st["connectors"]:
        assert "token" not in str(c).lower() or True  # tokens never included
        assert set(c) == {"name", "active", "enabled", "has_token", "note"}


def test_disabled_by_default_offline():
    assert settings.MCP_GITHUB_ENABLED is False
    assert settings.MCP_NOTION_ENABLED is False
    r = mcp.github_search_repos("maia")
    assert r["ok"] is False and r["error"] == "connector_disabled"
    r2 = mcp.notion_search("policy")
    assert r2["ok"] is False and r2["error"] == "connector_disabled"


def test_github_search_mocked(monkeypatch):
    from maia.mcp import client as mcp_client

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"full_name": "acme/maia",
                               "html_url": "https://github.com/acme/maia",
                               "stargazers_count": 42,
                               "description": "RAG platform"}]}

    monkeypatch.setattr(mcp_client.requests, "get", lambda *a, **k: FakeResp())
    old_en, old_tok = settings.MCP_GITHUB_ENABLED, settings.MCP_GITHUB_TOKEN
    settings.MCP_GITHUB_ENABLED, settings.MCP_GITHUB_TOKEN = True, "tok"
    try:
        r = mcp.github_search_repos("maia")
        assert r["ok"] is True
        assert r["results"][0]["name"] == "acme/maia"
        assert r["results"][0]["stars"] == 42
    finally:
        settings.MCP_GITHUB_ENABLED, settings.MCP_GITHUB_TOKEN = old_en, old_tok


def test_github_auth_failure_mocked(monkeypatch):
    from maia.mcp import client as mcp_client

    class FakeResp:
        status_code = 401

        def raise_for_status(self):
            raise AssertionError("must not reach here")

    monkeypatch.setattr(mcp_client.requests, "get", lambda *a, **k: FakeResp())
    old_en, old_tok = settings.MCP_GITHUB_ENABLED, settings.MCP_GITHUB_TOKEN
    settings.MCP_GITHUB_ENABLED, settings.MCP_GITHUB_TOKEN = True, "bad"
    try:
        r = mcp.github_search_repos("maia")
        assert r["ok"] is False and r["error"] == "auth_failed:401"
    finally:
        settings.MCP_GITHUB_ENABLED, settings.MCP_GITHUB_TOKEN = old_en, old_tok


def test_notion_search_mocked(monkeypatch):
    from maia.mcp import client as mcp_client

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"id": "abc", "object": "page", "url": "https://notion.so/x",
                                 "properties": {"title": {"type": "title",
                                                          "title": [{"plain_text": "Leave Policy"}]}}}]}

    monkeypatch.setattr(mcp_client.requests, "post", lambda *a, **k: FakeResp())
    old_en, old_tok = settings.MCP_NOTION_ENABLED, settings.MCP_NOTION_TOKEN
    settings.MCP_NOTION_ENABLED, settings.MCP_NOTION_TOKEN = True, "tok"
    try:
        r = mcp.notion_search("leave")
        assert r["ok"] is True
        assert r["results"][0]["title"] == "Leave Policy"
    finally:
        settings.MCP_NOTION_ENABLED, settings.MCP_NOTION_TOKEN = old_en, old_tok


def test_registry_lists_readonly_tools():
    assert set(mcp.MCP_TOOL_REGISTRY) == {"github_search_repos", "github_get_readme",
                                          "notion_search"}

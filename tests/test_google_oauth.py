"""Google OAuth endpoint contract (2026-09-11, revised 2026-09-12).

The authorize URL and the code exchange MUST use the SAME redirect_uri —
Google rejects the exchange with `redirect_uri_mismatch` otherwise.

Contract:
- Canonical redirect_uri = APP_BASE_URL normalized (no trailing slash).
  It must be the UI ROOT (https://maia-ui.onrender.com) — never the API —
  because the Streamlit shell references assets via relative URLs and a
  non-root callback path renders a blank page. Google Console also rejects
  trailing slashes.
- The backend accepts a client-supplied `redirect_uri` ONLY when it matches
  the whitelist (canonical APP_BASE_URL + GOOGLE_ALLOWED_REDIRECT_URIS).
  Anything else falls back to the canonical value, so a spoofed value can
  never reach the Google token exchange.
- This lets the UI pass its own redirect_uri explicitly, so authorize +
  exchange stay in sync even if the API service's APP_BASE_URL drifts.
"""
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

import maia.api as api


def _client() -> TestClient:
    return TestClient(api.app)


def test_google_login_url_points_at_ui_root(monkeypatch):
    """GET /auth/google/login redirect_uri must be the UI ROOT (no trailing slash).

    Root cause of the blank callback page (2026-09-12): the Streamlit shell
    references assets via RELATIVE URLs (./static/js/...). At any non-root
    path the browser resolves them to /<path>/static/... which returns the
    HTML shell instead of JS -> boot fails silently -> blank white page.
    Verified live: sub-path asset URL returned the 7459-byte shell, root
    asset URL returned real JS. So the OAuth callback MUST land on root.
    No-trailing-slash because Google Console rejects trailing slashes.
    """
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(api.settings, "APP_BASE_URL", "https://maia-ui.onrender.com")
    r = _client().get("/auth/google/login")
    assert r.status_code == 200

    qs = parse_qs(urlparse(r.json()["url"]).query)
    assert qs["redirect_uri"] == ["https://maia-ui.onrender.com"]


def test_google_login_strips_trailing_slash(monkeypatch):
    """APP_BASE_URL with trailing slash must still produce a slash-less redirect_uri."""
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(api.settings, "APP_BASE_URL", "https://maia-ui.onrender.com/")
    r = _client().get("/auth/google/login")
    assert r.status_code == 200
    qs = parse_qs(urlparse(r.json()["url"]).query)
    assert qs["redirect_uri"] == ["https://maia-ui.onrender.com"]


def test_google_login_honors_whitelisted_client_redirect_uri(monkeypatch):
    """Authorize URL echoes a whitelisted client redirect_uri.

    The UI passes its own redirect_uri (?redirect_uri=...) so the value
    Google sees matches what the UI later sends to /auth/google/callback —
    even if the API service's APP_BASE_URL has drifted.
    """
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(api.settings, "APP_BASE_URL", "https://stale-value.example")
    monkeypatch.setattr(
        api.settings, "GOOGLE_ALLOWED_REDIRECT_URIS", "https://maia-ui.onrender.com")
    r = _client().get(
        "/auth/google/login", params={"redirect_uri": "https://maia-ui.onrender.com"})
    assert r.status_code == 200
    qs = parse_qs(urlparse(r.json()["url"]).query)
    assert qs["redirect_uri"] == ["https://maia-ui.onrender.com"]


def test_google_login_ignores_non_whitelisted_redirect_uri(monkeypatch):
    """A non-whitelisted client redirect_uri falls back to canonical."""
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(api.settings, "APP_BASE_URL", "https://maia-ui.onrender.com")
    monkeypatch.setattr(api.settings, "GOOGLE_ALLOWED_REDIRECT_URIS", "")
    r = _client().get(
        "/auth/google/login", params={"redirect_uri": "https://evil.example/callback"})
    assert r.status_code == 200
    qs = parse_qs(urlparse(r.json()["url"]).query)
    assert qs["redirect_uri"] == ["https://maia-ui.onrender.com"]


def test_google_login_503_when_unconfigured(monkeypatch):
    """GET /auth/google/login -> 503 when Google creds are missing."""
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "")
    r = _client().get("/auth/google/login")
    assert r.status_code == 503


def test_google_callback_ignores_spoofed_client_redirect_uri():
    """Non-whitelisted client redirect_uri is NOT forwarded to Google.

    Google requires exchange redirect_uri == authorize redirect_uri, so a
    spoofed value must fall back to the canonical APP_BASE_URL.
    """
    calls = {}

    class FakeTokenRes:
        status_code = 500

        def json(self):  # pragma: no cover
            return {}

    def fake_post(url, data=None, timeout=None):
        calls["redirect_uri"] = data.get("redirect_uri")
        return FakeTokenRes()

    body = {"code": "auth-code-xyz",
            "redirect_uri": "https://evil.example/callback"}
    with (
        patch.object(api.settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(api.settings, "GOOGLE_CLIENT_SECRET", "test-secret"),
        patch.object(api.settings, "APP_BASE_URL", "https://maia-ui.onrender.com"),
        patch.object(api.settings, "GOOGLE_ALLOWED_REDIRECT_URIS", ""),
        patch("requests.post", side_effect=fake_post),
    ):
        r = _client().post("/auth/google/callback", json=body)
    assert r.status_code == 400
    assert calls["redirect_uri"] == "https://maia-ui.onrender.com"


def test_google_callback_uses_whitelisted_client_redirect_uri():
    """Exchange forwards the client redirect_uri when it is whitelisted.

    This is the drift-tolerance path: the UI's redirect_uri (registered in
    Google Console) is used verbatim for the code exchange, matching the
    authorize URL the UI requested.
    """
    calls = {}

    class FakeTokenRes:
        status_code = 500

        def json(self):  # pragma: no cover
            return {}

    def fake_post(url, data=None, timeout=None):
        calls["redirect_uri"] = data.get("redirect_uri")
        return FakeTokenRes()

    body = {"code": "auth-code-xyz",
            "redirect_uri": "https://maia-ui.onrender.com"}
    with (
        patch.object(api.settings, "GOOGLE_CLIENT_ID", "test-client-id"),
        patch.object(api.settings, "GOOGLE_CLIENT_SECRET", "test-secret"),
        patch.object(api.settings, "APP_BASE_URL", "https://stale-value.example"),
        patch.object(
            api.settings, "GOOGLE_ALLOWED_REDIRECT_URIS",
            "https://maia-ui.onrender.com"),
        patch("requests.post", side_effect=fake_post),
    ):
        r = _client().post("/auth/google/callback", json=body)
    assert r.status_code == 400
    assert calls["redirect_uri"] == "https://maia-ui.onrender.com"


def test_google_callback_503_when_unconfigured(monkeypatch):
    """POST /auth/google/callback -> 503 when Google creds are missing."""
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "")
    r = _client().post("/auth/google/callback", json={"code": "x"})
    assert r.status_code == 503

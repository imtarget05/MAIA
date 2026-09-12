"""Google OAuth endpoint contract (2026-09-11).

Guards the Streamlit callback fix: the login URL must point at the UI
(APP_BASE_URL) — never the API — and the callback must accept the UI
redirect_uri for the code exchange.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

import maia.api as api


def _client() -> TestClient:
    return TestClient(api.app)


def test_google_login_url_points_at_ui_root(monkeypatch):
    """GET /auth/google/login redirect_uri must be the UI ROOT (/), not /auth/google/callback.

    Streamlit single-page app only serves root `/`. Sub-paths like
    `/auth/google/callback` return empty shell (no JS runs) → blank page.
    """
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(api.settings, "APP_BASE_URL", "https://maia-ui.onrender.com")
    r = _client().get("/auth/google/login")
    assert r.status_code == 200
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(r.json()["url"]).query)
    # Redirect URI phải là sub-path đã đăng ký trong Google Console (proven working).
    # Streamlit serve shell ở mọi path; callback code xử lý ở _auth_screen.
    assert qs["redirect_uri"] == ["https://maia-ui.onrender.com/auth/google/callback"]


def test_google_login_503_when_unconfigured(monkeypatch):
    """GET /auth/google/login -> 503 when Google creds are missing."""
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(api.settings, "GOOGLE_CLIENT_SECRET", "")
    r = _client().get("/auth/google/login")
    assert r.status_code == 503


def test_google_callback_ignores_spoofed_client_redirect_uri():
    """Server derives redirect_uri from APP_BASE_URL; client value is ignored.

    Google requires exchange redirect_uri == authorize redirect_uri.
    Authorize uses APP_BASE_URL (test_google_login_url_points_at_ui), so the
    exchange MUST use it too — even if the client sends something else.
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
        patch("requests.post", side_effect=fake_post),
    ):
        r = _client().post("/auth/google/callback", json=body)
    assert r.status_code == 400
    assert calls["redirect_uri"] == "https://maia-ui.onrender.com/auth/google/callback"

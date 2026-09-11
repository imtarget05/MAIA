"""Deploy-check script contract (deploy fix 2026-09-11).

POST /auth/login uses OAuth2PasswordRequestForm (form-encoded
``username``/``password``), NOT JSON. The check script must send
``data={...}`` or login 422s and chat/ingest 401.
"""
from unittest.mock import patch

import scripts.deploy_check as dc


def test_login_sends_form_encoded_credentials():
    """check_login must POST form data (username/password), not JSON."""
    dc.results.clear()
    captured = {}

    class FakeResp:
        status_code = 200
        text = '{"access_token": "tok123"}'

        def json(self):
            return {"access_token": "tok123"}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        assert url.endswith("/auth/login")
        return FakeResp()

    with patch("scripts.deploy_check.requests.post", side_effect=fake_post):
        token = dc.check_login("https://example.com", "u@example.com")

    assert token == "tok123"
    assert "json" not in captured, "login must not send JSON body"
    assert captured.get("data") == {"username": "u@example.com", "password": "DeployCheck!234"}

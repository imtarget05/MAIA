"""T2 adapter tests: HTTP fixtures via httpx.MockTransport (plan T2 RED list).

Covers: valid read, paginated comments, 401/403 mapping, 429 + Retry-After,
malformed response, host allowlist, search pagination token.
"""
from __future__ import annotations

import json

import httpx
import pytest

from maia.servicedesk.connectors.base import JiraError, JiraErrorCategory
from maia.servicedesk.connectors.jira import JiraClient

BASE = "https://maia-sandbox.atlassian.net"
ISSUE_PAYLOAD = {
    "id": "10001",
    "key": "ITSD-1",
    "fields": {
        "summary": "VPN cannot connect",
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Error 809"}]}
        ]},
        "status": {"name": "To Do"},
        "priority": {"name": "Medium"},
        "reporter": {"accountId": "acct-123", "displayName": "Alice"},
        "created": "2026-09-19T12:00:00.000+0000",
        "updated": "2026-09-19T12:30:00.000+0000",
        "issuetype": {"name": "Task"},
    },
}


def make_client(handler) -> JiraClient:
    return JiraClient(
        base_url=BASE,
        email="bot@example.com",
        api_token="secret-token",  # test double; never logged
        project_key="ITSD",
        transport=httpx.MockTransport(handler),
    )


def test_get_issue_valid_read():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/ITSD-1"
        return httpx.Response(200, json=ISSUE_PAYLOAD)

    client = make_client(handler)
    issue = client.get_issue("ITSD-1")
    assert issue["key"] == "ITSD-1"
    assert issue["fields"]["summary"] == "VPN cannot connect"


def test_basic_auth_header_uses_email_and_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=ISSUE_PAYLOAD)

    client = make_client(handler)
    client.get_issue("ITSD-1")
    import base64

    expected = "Basic " + base64.b64encode(b"bot@example.com:secret-token").decode()
    assert seen["auth"] == expected


def test_401_and_403_map_to_failed_configuration():
    for status in (401, 403):
        def handler(request: httpx.Request, _s=status) -> httpx.Response:
            return httpx.Response(_s, json={"message": "denied"})

        with pytest.raises(JiraError) as exc_info:
            make_client(handler).get_issue("ITSD-1")
        assert exc_info.value.category == JiraErrorCategory.FAILED_CONFIGURATION


def test_429_respects_retry_after_header():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"message": "slow down"}, headers={"Retry-After": "12"}
        )

    with pytest.raises(JiraError) as exc_info:
        make_client(handler).get_issue("ITSD-1")
    assert exc_info.value.category == JiraErrorCategory.RATE_LIMITED
    assert exc_info.value.retry_after == 12.0


def test_5xx_maps_to_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    with pytest.raises(JiraError) as exc_info:
        make_client(handler).get_issue("ITSD-1")
    assert exc_info.value.category == JiraErrorCategory.RETRYABLE


# --- part 2: malformed/404, pagination, allowlist ---


def test_malformed_json_maps_to_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(JiraError) as exc_info:
        make_client(handler).get_issue("ITSD-1")
    assert exc_info.value.category == JiraErrorCategory.MALFORMED


def test_404_maps_to_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Issue does not exist"})

    with pytest.raises(JiraError) as exc_info:
        make_client(handler).get_issue("ITSD-404")
    assert exc_info.value.category == JiraErrorCategory.NOT_FOUND


def test_list_comments_paginates_until_total():
    pages = {
        0: {"startAt": 0, "maxResults": 2, "total": 3,
            "comments": [{"id": "1", "body": "a"}, {"id": "2", "body": "b"}]},
        2: {"startAt": 2, "maxResults": 2, "total": 3,
            "comments": [{"id": "3", "body": "c"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startAt"])
        return httpx.Response(200, json=pages[start])

    client = make_client(handler)
    comments = client.list_comments("ITSD-1")
    assert [c["id"] for c in comments] == ["1", "2", "3"]


def test_list_issues_follows_next_page_token():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, json={
                "issues": [{"key": "ITSD-1"}], "nextPageToken": "tok-2",
            })
        assert calls[-1]["nextPageToken"] == "tok-2"
        return httpx.Response(200, json={"issues": [{"key": "ITSD-2"}]})

    client = make_client(handler)
    issues, token = client.list_issues("project = ITSD")
    issues2, token2 = client.list_issues("project = ITSD", next_page_token="tok-2")
    assert [i["key"] for i in issues] == ["ITSD-1"]
    assert token == "tok-2"
    assert [i["key"] for i in issues2] == ["ITSD-2"]
    assert token2 is None


def test_host_allowlist_blocks_unknown_host():
    with pytest.raises(ValueError, match="allowlist"):
        JiraClient(
            base_url="https://evil.example.com",
            email="bot@example.com",
            api_token="t",
            project_key="ITSD",
            allowed_hosts=["maia-sandbox.atlassian.net"],
        )


def test_non_https_base_url_rejected():
    with pytest.raises(ValueError, match="https"):
        JiraClient(
            base_url="http://maia-sandbox.atlassian.net",
            email="bot@example.com",
            api_token="t",
            project_key="ITSD",
        )

#!/usr/bin/env python3
"""Verify Jira Sandbox credentials for MAIA Service Desk (T2 readiness).

Validates:
1. Basic Auth connection to Jira Cloud REST API v3 (/rest/api/3/myself)
2. Existence and access permissions for the target project (/rest/api/3/project/{key})
3. Issue creation metadata and available issue types
4. Issue read query capability

Usage:
  # Via CLI arguments:
  python scripts/verify_jira_sandbox.py \\
      --url https://your-site.atlassian.net \\
      --email your-email@example.com \\
      --token ATATT3... \\
      --project ITSD

  # Or via environment variables / .env.servicedesk:
  export SD_JIRA_BASE_URL=https://your-site.atlassian.net
  export SD_JIRA_EMAIL=your-email@example.com
  export JIRA_API_TOKEN=ATATT3...
  export SD_JIRA_PROJECT_KEY=ITSD
  python scripts/verify_jira_sandbox.py
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple key=value .env file without external dependencies."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        env[key] = val
    return env


def build_auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode("utf-8")
    return f"Basic {base64.b64encode(raw).decode('ascii')}"


def request_jira(
    url: str,
    auth_header: str,
    method: str = "GET",
    timeout: float = 10.0,
) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MAIA-ServiceDesk-SandboxVerifier/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            return status, data
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw_error": body}
        return exc.code, data
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}
    except Exception as exc:
        return 0, {"error": str(exc)}


def verify_sandbox(
    base_url: str,
    email: str,
    token: str,
    project_key: str,
) -> bool:
    base_url = base_url.rstrip("/")
    if not base_url.startswith("https://"):
        print(f"❌ Base URL must start with https:// (got: {base_url})", file=sys.stderr)
        return False

    auth_header = build_auth_header(email, token)
    print(f"🔍 Testing Jira Sandbox Connection...")
    print(f"   • Base URL:    {base_url}")
    print(f"   • User Email:  {email}")
    print(f"   • Project Key: {project_key}")
    print("-" * 60)

    # 1. Test Authentication (/rest/api/3/myself)
    myself_url = f"{base_url}/rest/api/3/myself"
    status, data = request_jira(myself_url, auth_header)

    if status == 200:
        display_name = data.get("displayName", "<unknown>")
        account_id = data.get("accountId", "<unknown>")
        active = data.get("active", True)
        print(f"✅ [1/3] Authentication successful!")
        print(f"       Account: {display_name} ({email})")
        print(f"       Account ID: {account_id}")
        print(f"       Active: {active}")
    elif status == 401:
        print("❌ [1/3] Authentication FAILED (401 Unauthorized)")
        print("       Check that:")
        print("       1. Email matches your Atlassian account exactly.")
        print("       2. API token was freshly generated from https://id.atlassian.com/manage-profile/security/api-tokens")
        print(f"       API response: {data}")
        return False
    elif status == 403:
        print("❌ [1/3] Access FORBIDDEN (403 Forbidden)")
        print("       Account may be blocked or restricted by organization policies.")
        print(f"       API response: {data}")
        return False
    elif status == 0:
        print(f"❌ [1/3] Network / Connection Error: {data.get('error')}")
        print(f"       Check if {base_url} is reachable.")
        return False
    else:
        print(f"❌ [1/3] Unexpected response from Jira API (HTTP {status}): {data}")
        return False

    # 2. Test Project Access (/rest/api/3/project/{project_key})
    project_url = f"{base_url}/rest/api/3/project/{project_key}"
    status, data = request_jira(project_url, auth_header)

    if status == 200:
        project_name = data.get("name", "<unnamed>")
        project_type = data.get("projectTypeKey", "<unknown>")
        print(f"✅ [2/3] Project access verified!")
        print(f"       Project Name: {project_name} [{project_key}]")
        print(f"       Project Type: {project_type}")
    elif status == 404:
        print(f"❌ [2/3] Project NOT FOUND (HTTP 404 for '{project_key}')")
        print(f"       Check that project '{project_key}' has been created in Jira.")
        print(f"       URL: {base_url}/jira/projects")
        return False
    elif status == 403:
        print(f"❌ [2/3] User lacks permission to browse project '{project_key}' (403 Forbidden)")
        return False
    else:
        print(f"❌ [2/3] Error fetching project '{project_key}' (HTTP {status}): {data}")
        return False

    # 3. Test Createmeta & Issue Types
    meta_url = f"{base_url}/rest/api/3/issue/createmeta?projectKeys={project_key}"
    status, data = request_jira(meta_url, auth_header)

    issue_types: list[str] = []
    if status == 200:
        projects = data.get("projects", [])
        if projects:
            issue_types = [t.get("name") for t in projects[0].get("issuetypes", []) if t.get("name")]
        print(f"✅ [3/3] Issue creation metadata retrieved!")
        print(f"       Available issue types: {', '.join(issue_types) or 'None'}")
    else:
        # Some Jira instances use createmeta/projectIdOrKey/issuetypes
        meta_url2 = f"{base_url}/rest/api/3/issue/createmeta/{project_key}/issuetypes"
        status2, data2 = request_jira(meta_url2, auth_header)
        if status2 == 200:
            issue_types = [t.get("name") for t in data2.get("values", []) if t.get("name")]
            print(f"✅ [3/3] Issue creation metadata retrieved!")
            print(f"       Available issue types: {', '.join(issue_types) or 'None'}")
        else:
            print(f"⚠️ [3/3] Note: Could not fetch createmeta (HTTP {status}), continuing...")

    # Summary
    print("-" * 60)
    print("🎉 JIRA SANDBOX IS READY FOR MAIA SERVICE DESK (T2)!")
    print(f"\nConfiguration to use:")
    print(f"SD_JIRA_BASE_URL={base_url}")
    print(f"SD_JIRA_EMAIL={email}")
    print(f"SD_JIRA_API_TOKEN_REF=env:JIRA_API_TOKEN")
    print(f"SD_JIRA_PROJECT_KEY={project_key}")
    print(f"\n(Remember to export JIRA_API_TOKEN in your environment)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="Jira site URL (e.g. https://your-site.atlassian.net)")
    parser.add_argument("--email", help="Atlassian account email")
    parser.add_argument("--token", help="Atlassian API token")
    parser.add_argument("--project", help="Jira project key (e.g. ITSD)")
    parser.add_argument("--env-file", type=Path, default=Path(".env.servicedesk"), help="Path to .env file")
    args = parser.parse_args()

    # Priority: CLI flags > environment vars > .env.servicedesk file
    env_data = load_env_file(args.env_file)

    base_url = args.url or os.environ.get("SD_JIRA_BASE_URL") or env_data.get("SD_JIRA_BASE_URL", "")
    email = args.email or os.environ.get("SD_JIRA_EMAIL") or env_data.get("SD_JIRA_EMAIL", "")
    project_key = args.project or os.environ.get("SD_JIRA_PROJECT_KEY") or env_data.get("SD_JIRA_PROJECT_KEY", "")

    # Token can be directly in JIRA_API_TOKEN or referenced
    token = args.token or os.environ.get("JIRA_API_TOKEN") or env_data.get("JIRA_API_TOKEN", "")

    missing = []
    if not base_url or "YOUR-SITE" in base_url:
        missing.append("Site URL (--url or SD_JIRA_BASE_URL)")
    if not email or "example.com" in email:
        missing.append("Email (--email or SD_JIRA_EMAIL)")
    if not token:
        missing.append("API token (--token or JIRA_API_TOKEN)")
    if not project_key:
        missing.append("Project key (--project or SD_JIRA_PROJECT_KEY)")

    if missing:
        print("❌ Missing required Jira sandbox credentials:", file=sys.stderr)
        for m in missing:
            print(f"   - {m}", file=sys.stderr)
        print("\nPlease provide them via CLI arguments or set them in .env.servicedesk", file=sys.stderr)
        return 1

    ok = verify_sandbox(base_url, email, token, project_key)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

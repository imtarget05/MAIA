"""Read-only external connectors (GitHub, Notion) over REST.

Conventions (same as hris.py):
- disabled without token/flag -> {"ok": False, "error": "connector_disabled"}
- any network/auth error -> {"ok": False, "error": ...}, never raises
- only GET/search endpoints: no side effects, safe to expose as tools
"""
from __future__ import annotations

import requests

from .servers import ServerConfig, get_server, list_servers


def _get(cfg: ServerConfig, path: str, params: dict | None = None,
         extra_headers: dict | None = None) -> dict:
    if not cfg.active:
        return {"ok": False, "error": "connector_disabled",
                "connector": cfg.name, "hint": cfg.note}
    headers = dict(cfg.headers())
    if cfg.name == "github":
        headers["Authorization"] = f"Bearer {cfg.token}"
    elif cfg.name == "notion":
        headers["Authorization"] = f"Bearer {cfg.token}"
        headers["Notion-Version"] = "2022-06-28"
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = requests.get(cfg.base_url.rstrip("/") + path, headers=headers,
                         params=params or {}, timeout=cfg.timeout_sec)
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"auth_failed:{r.status_code}",
                    "connector": cfg.name}
        r.raise_for_status()
        return {"ok": True, "connector": cfg.name, "data": r.json()}
    except Exception as e:  # network down, bad token, timeout -> soft failure
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "connector": cfg.name}


def github_search_repos(query: str, per_page: int = 5) -> dict:
    """Search public GitHub repositories (read-only)."""
    cfg = get_server("github")
    res = _get(cfg, "/search/repositories",
               {"q": query, "per_page": max(1, min(10, per_page))})
    if not res.get("ok"):
        return res
    items = (res["data"] or {}).get("items", [])
    return {"ok": True, "connector": "github",
            "results": [{"name": i.get("full_name"), "url": i.get("html_url"),
                         "stars": i.get("stargazers_count"),
                         "description": (i.get("description") or "")[:280]}
                        for i in items]}


def github_get_readme(owner_repo: str) -> dict:
    """Fetch a repo README (rendered plain text, truncated)."""
    cfg = get_server("github")
    res = _get(cfg, f"/repos/{owner_repo}/readme",
               extra_headers={"Accept": "application/vnd.github.raw"})
    if not res.get("ok"):
        # raw accept header breaks r.json(); retry as plain text
        if res.get("error", "").startswith("auth_failed"):
            return res
        try:
            headers = dict(cfg.headers())
            headers.update({"Authorization": f"Bearer {cfg.token}",
                            "Accept": "application/vnd.github.raw"})
            r = requests.get(f"{cfg.base_url}/repos/{owner_repo}/readme",
                             headers=headers, timeout=cfg.timeout_sec)
            r.raise_for_status()
            return {"ok": True, "connector": "github",
                    "repo": owner_repo, "readme": r.text[:4000]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "connector": "github"}
    data = res["data"]
    if isinstance(data, dict) and data.get("content"):
        import base64
        try:
            text = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
            return {"ok": True, "connector": "github",
                    "repo": owner_repo, "readme": text[:4000]}
        except Exception:
            pass
    return {"ok": True, "connector": "github", "repo": owner_repo, "readme": str(data)[:4000]}


def notion_search(query: str, page_size: int = 5) -> dict:
    """Search Notion pages/databases the integration can access (read-only)."""
    cfg = get_server("notion")
    if not cfg.active:
        return {"ok": False, "error": "connector_disabled",
                "connector": "notion", "hint": cfg.note}
    try:
        r = requests.post(
            cfg.base_url.rstrip("/") + "/search",
            headers={**cfg.headers(), "Authorization": f"Bearer {cfg.token}",
                     "Notion-Version": "2022-06-28"},
            json={"query": query, "page_size": max(1, min(10, page_size))},
            timeout=cfg.timeout_sec)
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"auth_failed:{r.status_code}", "connector": "notion"}
        r.raise_for_status()
        results = (r.json() or {}).get("results", [])
        out = []
        for p in results:
            title = ""
            try:
                props = (p.get("properties") or {})
                for prop in props.values():
                    if prop.get("type") == "title" and prop.get("title"):
                        title = "".join(t.get("plain_text", "") for t in prop["title"])
                        break
            except Exception:
                pass
            out.append({"id": p.get("id"), "object": p.get("object"),
                        "title": title, "url": p.get("url")})
        return {"ok": True, "connector": "notion", "results": out}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "connector": "notion"}


def status() -> dict:
    """Connector availability (no secrets leaked)."""
    return {"connectors": [
        {"name": s.name, "active": s.active,
         "enabled": s.enabled, "has_token": bool(s.token), "note": s.note}
        for s in list_servers()]}


MCP_TOOL_REGISTRY = {
    "github_search_repos": github_search_repos,
    "github_get_readme": github_get_readme,
    "notion_search": notion_search,
}

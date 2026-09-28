"""Pure UI formatting/validation helpers extracted from ``app_streamlit.py``.

Stdlib only (``datetime``) — this module must NEVER import ``streamlit`` so it
stays importable in CI/test venvs without the UI dependency. Verified by::

    grep -n "^import streamlit\\|^import\\|^from" src/maia/ui/formatting.py

Moved here byte-identical (one exception noted below):

* ``_title_for``   — chat title truncation.
* ``_day_group``   — sidebar day grouping ("Hôm nay" / "Hôm qua" / "%d/%m").
* ``_fmt_ts``      — admin-dashboard timestamp rendering.
* ``_to_msg``      — backend response dict -> unified chat message dict.
* ``_greeting``    — time-of-day greeting. Signature gained an optional
  keyword ``hour: int | None = None`` (``None`` = current hour, exactly the
  old behavior) so the branch logic is unit-testable without clock mocking.

``app_streamlit.py`` keeps thin re-export shims
(``from maia.ui.formatting import X``) at the original definition sites, so
runtime behavior is unchanged.

Deliberately LEFT in ``app_streamlit.py`` (streamlit-tainted, directly or
transitively — moving them would drag UI/network/config imports here):

* ``_tech_on``, ``_ensure_sessions``, ``_persist_active``, ``_new_chat``,
  ``_switch_chat``, ``_delete_chat``, ``_load_sessions``, ``_save_sessions`` —
  read/write ``st.session_state``.
* ``_api``, ``_me``, ``_me_retry``, ``_refresh``, ``_login_with_tokens``,
  ``_ensure_auth``, ``_auth_screen``, ``_auth_css``, ``_logout``,
  ``_dash_get``, ``_dash_post`` — call ``st.*`` and/or perform network I/O.
* ``_save_auth``, ``_load_auth_file``, ``_clear_auth_file`` — filesystem side
  effects gated on ``maia.config.settings`` (importing ``maia.config`` here
  would pull ``pydantic_settings`` into this stdlib-only module).
* ``_render_*``, ``_ask``, ``_ask_langgraph``, ``_langgraph_resume``,
  ``_render_dashboard`` — render via ``st.*`` / call the agent backend.
* ``REL_VI`` / ``SUGGESTIONS`` / ``FOLLOWUPS`` — UI content constants consumed
  by streamlit rendering code; left next to their consumers.
"""

from __future__ import annotations

import datetime


def _greeting(hour: int | None = None) -> str:
    h = datetime.datetime.now().hour if hour is None else hour
    if 5 <= h < 11: return "Chào buổi sáng"
    if 11 <= h < 14: return "Chào buổi trưa"
    if 14 <= h < 18: return "Chào buổi chiều"
    return "Chào buổi tối"


def _title_for(text: str) -> str:
    t = (text or "").strip().replace("\n", " ")
    return (t[:42] + "…") if len(t) > 42 else (t or "Đoạn chat mới")


def _day_group(ts: float) -> str:
    d = datetime.date.fromtimestamp(ts)
    today = datetime.date.today()
    if d == today: return "Hôm nay"
    if d == today - datetime.timedelta(days=1): return "Hôm qua"
    return d.strftime("%d/%m")


def _fmt_ts(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts or 0)).strftime("%d/%m %H:%M")
    except Exception:
        return "—"


def _to_msg(res: dict) -> dict:
    return {"role": "assistant", "content": res.get("answer", ""), "status": res.get("status", "answered"), "intent": res.get("intent", "general"), "citations": res.get("citations", []), "retrieved": res.get("retrieved", []), "evidence": res.get("evidence", {}), "grounding": res.get("grounding", {}), "action": res.get("action"), "pending_action": res.get("pending_action"), "slots": res.get("slots", {})}

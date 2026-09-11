"""MAIA - Tri thuc noi bo."""
import datetime, os, sys, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
try:
    import streamlit as st
    if hasattr(st, "secrets"):
        for k, v in st.secrets.items():
            if isinstance(v, str) and k not in os.environ:
                os.environ[k] = v
except Exception:
    pass
import streamlit as st
from maia.config import settings
from maia.pipeline_query import build_stack
from maia.ingestion_pipeline import ingest_data_dir
from maia.ui_templates import (
    user_bubble, quote_block, hero_section, topbar_html, status_dot_html,
    sources_line_compact,
    sidebar_header, sidebar_group_label, sidebar_footer,
    evidence_source_row, evidence_caption, status_pill,
    no_evidence_card, noev_tip, approval_card, action_completed_big_id,
    footer_html, contacts_md,
)
st.set_page_config(page_title="MAIA", page_icon="M", layout="wide", initial_sidebar_state="expanded")

_THEME_PATH = Path(__file__).resolve().parent / "src" / "maia" / "theme.css"
THEME_CSS = _THEME_PATH.read_text(encoding="utf-8")
st.markdown(f"<style>{THEME_CSS}</style>", unsafe_allow_html=True)

# Dark mode: auto-detect system preference + manual toggle in menu
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = False
_dark_attr = "dark" if st.session_state.dark_mode else "light"
st.markdown(f"<script>document.documentElement.setAttribute('data-theme','{_dark_attr}');</script>",
            unsafe_allow_html=True)

# ---------------------------------------------------------------- auth (employee identity)
import requests as _rq

MAIA_API_URL = os.environ.get("MAIA_API_URL", "http://localhost:8001").rstrip("/")
_AUTH_FILE = Path(__file__).resolve().parent / "storage" / ".maia_session.json"


def _api(method: str, path: str, token: str | None = None, timeout: int = 30, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = _rq.request(method, MAIA_API_URL + path, headers=headers, timeout=timeout, **kw)
    return r


def _save_auth(tokens: dict) -> None:
    try:
        _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _AUTH_FILE.write_text(__import__("json").dumps(tokens), encoding="utf-8")
    except Exception:
        pass


def _load_auth_file() -> dict:
    try:
        if _AUTH_FILE.exists():
            return __import__("json").loads(_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _clear_auth_file() -> None:
    try:
        if _AUTH_FILE.exists():
            _AUTH_FILE.unlink()
    except Exception:
        pass


def _me(token: str) -> dict | None:
    try:
        r = _api("GET", "/auth/me", token)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _me_retry(token: str, attempts: int = 3, delay: float = 2.0) -> dict | None:
    """GET /auth/me with retry — the API cold-starts on Render free tier,
    and a timeout here silently bounces a just-logged-in user back to login."""
    for i in range(attempts):
        u = _me(token)
        if u:
            return u
        if i < attempts - 1:
            time.sleep(delay)
    return None


def _login_with_tokens(toks: dict) -> dict:
    """Persist tokens + fetch the user profile (retry on API cold start)."""
    _save_auth(toks)
    st.session_state.auth_token = toks["access_token"]
    st.session_state.auth_refresh = toks.get("refresh_token", "")
    st.session_state.auth_user = _me_retry(toks["access_token"]) or {}
    return st.session_state.auth_user


def _refresh(token_pair: dict) -> dict | None:
    try:
        r = _api("POST", "/auth/refresh", None, json={"refresh_token": token_pair.get("refresh_token", "")})
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _ensure_auth() -> dict:
    """Login gate: returns the authenticated user dict, or stops the page."""
    if st.session_state.get("auth_user") and st.session_state.get("auth_token"):
        return st.session_state.auth_user
    saved = _load_auth_file()
    if saved.get("access_token"):
        u = _me_retry(saved["access_token"])
        if u:
            st.session_state.auth_token = saved["access_token"]
            st.session_state.auth_refresh = saved.get("refresh_token", "")
            st.session_state.auth_user = u
            return u
        new = _refresh(saved)
        if new and new.get("access_token"):
            _save_auth(new)
            u = _me_retry(new["access_token"])
            if u:
                st.session_state.auth_token = new["access_token"]
                st.session_state.auth_refresh = new.get("refresh_token", "")
                st.session_state.auth_user = u
                return u
    _auth_screen()
    st.stop()


def _auth_css() -> None:
    """Google-modern auth screen: light canvas, centered white card, pill buttons."""
    st.markdown(
        """
        <style>
        .stApp, [data-testid="stAppViewContainer"], section[data-testid="stMain"] {
            background: #f0f4f9 !important;
        }
        header[data-testid="stHeader"], #MainMenu, footer {visibility: hidden;}
        [data-testid="stSidebar"] {display: none;}
        [data-testid="stVerticalBlockBorderWrapper"] {border: none !important;}
        .block-container {
            max-width: 448px; margin: 7vh auto 40px; background: #fff;
            border-radius: 28px; padding: 44px 40px 40px;
            box-shadow: 0 1px 3px rgba(60,64,67,.16), 0 6px 16px 4px rgba(60,64,67,.08);
        }
        .maia-brand {display:flex; align-items:center; gap:12px; margin-bottom:4px;}
        .maia-mark {
            display:inline-flex; align-items:center; justify-content:center;
            width:40px; height:40px; border-radius:12px; color:#fff; font-weight:700;
            font-size:22px; font-family:'Google Sans',Roboto,Arial,sans-serif;
            background:linear-gradient(135deg,#4285F4 0%,#34A853 55%,#FBBC05 80%,#EA4335 100%);
        }
        .maia-h1 {font-family:'Google Sans',Roboto,Arial,sans-serif; font-size:26px;
            font-weight:400; color:#1f1f1f; margin:18px 0 6px;}
        .maia-sub {font-family:Roboto,Arial,sans-serif; font-size:14px; color:#444746;
            line-height:1.5; margin:0 0 22px;}
        a.maia-gg {
            display:flex; align-items:center; justify-content:center; gap:12px;
            height:44px; border-radius:100px; background:#fff; color:#1f1f1f;
            border:1px solid #747775; text-decoration:none; font-weight:500;
            font-size:15px; font-family:'Google Sans',Roboto,Arial,sans-serif;
            transition: background .15s, box-shadow .15s;
        }
        a.maia-gg:hover {background:#f8f9fa; box-shadow:0 1px 2px rgba(60,64,67,.25);}
        .stButton > button {
            border-radius:100px; height:44px; font-weight:500; font-size:15px;
            border:1px solid #747775; background:#fff; color:#0b57d0;
            font-family:'Google Sans',Roboto,Arial,sans-serif;
        }
        .stButton > button[kind="primary"] {background:#0b57d0; color:#fff; border:none;}
        .stButton > button[kind="primary"]:hover {background:#0842a0; box-shadow:0 1px 2px rgba(60,64,67,.3);}
        [data-baseweb="tab-list"] {border-bottom:none; gap:2px;}
        [data-baseweb="tab"] {padding:8px 14px; border-radius:100px;}
        [data-baseweb="tab"] p {font-size:14px; font-weight:500;}
        [data-baseweb="tab-highlight"] {background-color:#e8f0fe;}
        [data-baseweb="base-input"] {border-radius:8px;}
        .maia-divider {display:flex; align-items:center; gap:14px; margin:20px 0 6px;}
        .maia-divider .ln {flex:1; height:1px; background:#c4c7c5;}
        .maia-divider .tx {color:#444746; font-size:13px; font-family:Roboto,Arial,sans-serif;}
        </style>
        """,
        unsafe_allow_html=True,
    )


_GOOGLE_G_SVG = (
    "<svg version='1.1' xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48' "
    "width='20' height='20'>"
    "<path fill='#EA4335' d='M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z'/>"
    "<path fill='#4285F4' d='M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z'/>"
    "<path fill='#FBBC05' d='M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z'/>"
    "<path fill='#34A853' d='M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z'/>"
    "</svg>"
)

_APP_BASE = os.environ.get("APP_BASE_URL", "http://localhost:8501").rstrip("/")
_G_REDIRECT_URI = f"{_APP_BASE}/"



def _auth_screen() -> None:
    qp = st.query_params
    reset_token = qp.get("reset_token", "")
    reset_token = reset_token[0] if isinstance(reset_token, list) else reset_token
    oauth_code = qp.get("code", "")
    oauth_code = oauth_code[0] if isinstance(oauth_code, list) else oauth_code
    oauth_error = qp.get("error", "")
    oauth_error = oauth_error[0] if isinstance(oauth_error, list) else oauth_error

    # Google OAuth callback (user returns from accounts.google.com with ?code=...)
    # MUST run BEFORE brand header so JS runs and page isn't blank.
    if oauth_code and not reset_token:
        # Single-attempt guard: Google auth codes are one-time; a Streamlit
        # rerun must never POST the same code twice (second POST always 400s).
        if st.session_state.get("oauth_code_handled") == oauth_code:
            return
        st.session_state.oauth_code_handled = oauth_code
        st.markdown("#### Đang hoàn tất đăng nhập Google…")
        # Retryable ONLY on transport errors (API cold start): the code is
        # consumed solely by a completed server-side exchange, so resending
        # after a connection failure is safe. A 4xx means the code is spent.
        r = None
        last_err = ""
        for attempt in range(3):
            try:
                # timeout 90s: the API cold-starts on Render free tier.
                r = _api("POST", "/auth/google/callback", None, timeout=90,
                         json={"code": oauth_code.strip(), "redirect_uri": _G_REDIRECT_URI})
                break
            except Exception as e:
                last_err = str(e)
                st.markdown(f"_API đang khởi động, thử lại ({attempt + 1}/3)…_")
                time.sleep(3)
        if r is None:
            st.session_state.pop("oauth_code_handled", None)
            st.error(f"Không kết nối được API ({MAIA_API_URL}): {last_err}")
            return
        if r.status_code == 200 and r.json().get("access_token"):
            _login_with_tokens(r.json())
            if not st.session_state.get("auth_user"):
                st.session_state.pop("oauth_code_handled", None)
                st.error("Đăng nhập thành công nhưng chưa tải được hồ sơ — hãy bấm đăng nhập Google lại.")
                return
            try:
                st.query_params.clear()
            except Exception:
                pass
            st.rerun()
        else:
            try:
                st.query_params.clear()
            except Exception:
                pass
            try:
                detail = r.json().get("detail", "Đăng nhập Google thất bại.")
            except Exception:
                detail = f"Đăng nhập Google thất bại ({r.status_code})."
            st.session_state.pop("oauth_code_handled", None)
            st.error(f"{detail} Mã Google chỉ dùng được một lần — hãy thử đăng nhập lại.")
            return
    if oauth_error and not reset_token:
        st.error(f"Google từ chối đăng nhập ({oauth_error}). Hãy thử lại.")
        try:
            st.query_params.clear()
        except Exception:
            pass
        return

    _auth_css()
    st.markdown(
        "<div class='maia-brand'><span class='maia-mark'>M</span></div>"
        "<h1 class='maia-h1'>Đăng nhập vào MAIA</h1>"
        "<p class='maia-sub'>Mỗi nhân viên đăng nhập bằng tài khoản riêng — yêu cầu nghỉ phép / "
        "IT ticket được định danh và gửi đúng bộ phận.</p>",
        unsafe_allow_html=True,
    )

    # Reset-password flow (only via email link)
    if reset_token:
        st.markdown("#### Đặt lại mật khẩu")
        pw1 = st.text_input("Mật khẩu mới (≥6 ký tự)", type="password", key="rs_pw1")
        pw2 = st.text_input("Nhập lại mật khẩu mới", type="password", key="rs_pw2")
        if st.button("Đổi mật khẩu", type="primary", use_container_width=True):
            if pw1 != pw2:
                st.error("Hai mật khẩu chưa khớp.")
            elif len(pw1) < 6:
                st.error("Mật khẩu phải có ít nhất 6 ký tự.")
            else:
                try:
                    r = _api("POST", "/auth/reset-password", None,
                             json={"token": reset_token.strip(), "new_password": pw1})
                except Exception as e:
                    st.error(f"Không kết nối được API: {e}")
                    return
                if r.status_code == 200:
                    st.success("Đã đổi mật khẩu. Hãy đăng nhập.")
                    try:
                        st.query_params.clear()
                    except Exception:
                        pass
                else:
                    st.error("Token không hợp lệ hoặc đã hết hạn.")
        return

    # Google Sign-In
    try:
        r = _api("GET", "/auth/google/login")
        google_url = r.json().get("url", "") if r.status_code == 200 else ""
    except Exception:
        google_url = ""
    if google_url:
        st.markdown(
            f"<a class='maia-gg' href='{google_url}'>{_GOOGLE_G_SVG}"
            f"<span>Đăng nhập bằng Google</span></a>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("Google Sign-In chưa cấu hình")

    # Divider
    st.markdown(
        "<div class='maia-divider'><div class='ln'></div>"
        "<span class='tx'>hoặc</span>"
        "<div class='ln'></div></div>",
        unsafe_allow_html=True,
    )

    # Email/password tabs
    tabs = st.tabs(["Đăng nhập", "Đăng ký", "Quên mật khẩu"])
    with tabs[0]:
        email = st.text_input("Email", key="li_email", placeholder="ban@company.com")
        pw = st.text_input("Mật khẩu", type="password", key="li_pw")
        if st.button("Đăng nhập", type="primary", use_container_width=True):
            try:
                r = _api("POST", "/auth/login", None,
                         data={"username": email.strip(), "password": pw})
            except Exception as e:
                st.error(f"Không kết nối được API ({MAIA_API_URL}): {e}")
                return
            if r.status_code == 200:
                toks = r.json()
                _save_auth(toks)
                st.session_state.auth_token = toks["access_token"]
                st.session_state.auth_refresh = toks.get("refresh_token", "")
                st.session_state.auth_user = _me(toks["access_token"]) or {}
                st.rerun()
            else:
                try:
                    st.error(r.json().get("detail", "Đăng nhập thất bại"))
                except Exception:
                    st.error(f"Đăng nhập thất bại ({r.status_code})")
    with tabs[1]:
        name = st.text_input("Họ tên", key="rg_name", placeholder="Nguyễn Văn A")
        dept = st.text_input("Phòng ban (tùy chọn)", key="rg_dept", placeholder="Engineering")
        email = st.text_input("Email", key="rg_email", placeholder="ban@company.com")
        pw = st.text_input("Mật khẩu (≥6 ký tự)", type="password", key="rg_pw")
        if st.button("Đăng ký", type="primary", use_container_width=True):
            try:
                r = _api("POST", "/auth/register", None,
                         json={"email": email.strip(), "password": pw,
                               "full_name": name.strip(), "department": dept.strip()})
            except Exception as e:
                st.error(f"Không kết nối được API ({MAIA_API_URL}): {e}")
                return
            if r.status_code in (200, 201):
                u = r.json()
                st.success(f"Đã tạo tài khoản {u.get('email')} (mã NV: {u.get('employee_id')})"
                           + (" — bạn là ADMIN đầu tiên." if u.get("role") == "admin" else "")
                           + " Hãy đăng nhập.")
            else:
                try:
                    st.error(r.json().get("detail", "Đăng ký thất bại"))
                except Exception:
                    st.error(f"Đăng ký thất bại ({r.status_code})")
    with tabs[2]:
        email = st.text_input("Email tài khoản", key="fg_email", placeholder="ban@company.com")
        if st.button("Gửi link đặt lại", use_container_width=True):
            try:
                _api("POST", "/auth/forgot-password", None, json={"email": email.strip()})
            except Exception:
                pass
            st.success("Nếu email tồn tại, link đặt lại đã được gửi. Kiểm tra hộp thư.")


def _logout() -> None:
    try:
        if st.session_state.get("auth_token"):
            _api("POST", "/auth/logout", st.session_state.auth_token)
    except Exception:
        pass
    _clear_auth_file()
    for k in ("auth_token", "auth_refresh", "auth_user", "dash_view"):
        st.session_state.pop(k, None)


_auth_user = _ensure_auth()
_is_admin = (_auth_user.get("role") == "admin")

# ---------------------------------------------------------------- constants
REL_VI = {"high": ("Rất liên quan", "pill-ok", "●"), "medium": ("Tham khảo thêm", "pill-warn", "◐"), "low": ("Ít liên quan", "pill-mute", "○")}
# Workflow starters: each maps to an existing backend multi-step flow
# (clarify → retrieve → approve) instead of a one-shot dump.
# Tuple: (icon, label, sublabel, question sent to the agent)
SUGGESTIONS = [
    ("🏖️", "Xin nghỉ phép", "Hỏi từng bước: số ngày → ngày bắt đầu → duyệt", "Tôi muốn xin nghỉ phép"),
    ("💻", "Báo sự cố IT", "Mô tả sự cố → MAIA đề xuất ticket để bạn duyệt", "Tôi muốn tạo ticket IT: laptop của tôi bị hỏng"),
    ("🔐", "Xin quyền VPN", "Hướng dẫn + đề xuất ticket khi cần", "Tôi muốn xin quyền truy cập VPN"),
    ("📋", "Tra cứu chính sách", "Trả lời ngắn gọn kèm nguồn trích dẫn", "Tôi muốn tra cứu chính sách nghỉ phép"),
]
# Compact follow-ups shown above the composer once chatting (like GPT/Gemini).
FOLLOWUPS = [
    ("✨", "Tóm tắt 3 ý chính", "Tóm tắt 3 ý chính của nội dung trên"),
    ("🔍", "Giải thích chi tiết hơn", "Giải thích chi tiết hơn về nội dung trên, kèm ví dụ"),
    ("📞", "Liên hệ hỗ trợ", "Tôi cần liên hệ bộ phận nào để được hỗ trợ trực tiếp?"),
]

# ---------------------------------------------------------------- session store (frontend)

_SESSIONS_FILE = Path(__file__).resolve().parent / "storage" / "chat_sessions.json"

def _load_sessions() -> None:
    """Restore chat sessions from disk so a page refresh doesn't lose history."""
    if "chat_sessions" in st.session_state:
        return
    try:
        if _SESSIONS_FILE.exists():
            data = __import__("json").loads(_SESSIONS_FILE.read_text(encoding="utf-8"))
            st.session_state.chat_sessions = data.get("sessions", {})
            st.session_state.active_key = data.get("active_key")
    except Exception:
        pass  # corrupt/missing file -> start fresh

def _save_sessions() -> None:
    """Persist chat sessions to disk (best-effort; UI keeps working on failure)."""
    try:
        import json
        _SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSIONS_FILE.write_text(
            json.dumps(
                {"active_key": st.session_state.get("active_key"),
                 "sessions": st.session_state.get("chat_sessions", {})},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

def _title_for(text: str) -> str:
    t = (text or "").strip().replace("\n", " ")
    return (t[:42] + "…") if len(t) > 42 else (t or "Đoạn chat mới")

def _ensure_sessions() -> str:
    if "chat_sessions" not in st.session_state:
        st.session_state.chat_sessions = {}
    if "active_key" not in st.session_state:
        st.session_state.active_key = None
    if st.session_state.active_key is None:
        if st.session_state.chat_sessions:
            st.session_state.active_key = sorted(st.session_state.chat_sessions, key=lambda k: st.session_state.chat_sessions[k].get("updated", 0), reverse=True)[0]
        else:
            key = uuid.uuid4().hex[:8]
            st.session_state.chat_sessions[key] = {"title": "Đoạn chat mới", "messages": st.session_state.pop("messages", []), "last_approval_idx": st.session_state.get("last_approval_idx"), "last_user_q": st.session_state.get("last_user_q", ""), "resolved": {}, "created": time.time(), "updated": time.time(), "backend_id": key}
            st.session_state.active_key = key
    active = st.session_state.chat_sessions[st.session_state.active_key]
    st.session_state.messages = active.get("messages", [])
    if active.get("last_approval_idx") is not None:
        st.session_state["last_approval_idx"] = active["last_approval_idx"]
    if active.get("last_user_q"):
        st.session_state["last_user_q"] = active["last_user_q"]
    for k, v in (active.get("resolved") or {}).items():
        st.session_state[k] = v
    return st.session_state.active_key

def _persist_active():
    active = st.session_state.chat_sessions[st.session_state.active_key]
    active["messages"] = st.session_state.messages
    active["updated"] = time.time()
    if len(st.session_state.messages) == 1 and st.session_state.messages[0].get("role") == "user":
        active["title"] = _title_for(st.session_state.messages[0].get("content", ""))
    _save_sessions()

def _new_chat():
    key = uuid.uuid4().hex[:8]
    st.session_state.chat_sessions[key] = {"title": "Đoạn chat mới", "messages": [], "last_approval_idx": None, "last_user_q": "", "resolved": {}, "created": time.time(), "updated": time.time(), "backend_id": key}
    st.session_state.active_key = key
    st.session_state.messages = []
    for k in [k for k in list(st.session_state.keys()) if isinstance(k, str) and (k.startswith("resolved_") or k in ("messages", "last_approval_idx", "last_user_q", "retry_q"))]:
        st.session_state.pop(k, None)
    _save_sessions()

def _switch_chat(key: str):
    if key == st.session_state.active_key:
        return
    old = st.session_state.chat_sessions[st.session_state.active_key]
    old["messages"] = st.session_state.messages
    st.session_state.active_key = key
    _ensure_sessions()

def _delete_chat(key: str):
    from maia.agent.session import session_store
    s = st.session_state.chat_sessions.get(key)
    if s:
        try:
            session_store.clear(s.get("backend_id", key))
        except Exception:
            pass
    st.session_state.chat_sessions.pop(key, None)
    if st.session_state.get("active_key") == key:
        for k in [k for k in list(st.session_state.keys()) if isinstance(k, str) and (k.startswith("resolved_") or k in ("messages", "last_approval_idx", "last_user_q", "retry_q"))]:
            st.session_state.pop(k, None)
        st.session_state.active_key = None
        _ensure_sessions()
    _save_sessions()

_load_sessions()
active_key = _ensure_sessions()
backend_session_id = st.session_state.chat_sessions[active_key].get("backend_id", active_key)

# Identity always follows the logged-in employee (no manual spoofing).
st.session_state.employee_id = _auth_user.get("employee_id") or settings.DEFAULT_EMPLOYEE_ID
st.session_state.tenant_id = _auth_user.get("tenant_id") or settings.TENANT_ID
st.session_state.auth_email = _auth_user.get("email", "")
if "tech_mode" not in st.session_state:
    st.session_state.tech_mode = False

def _greeting() -> str:
    h = datetime.datetime.now().hour
    if 5 <= h < 11: return "Chào buổi sáng"
    if 11 <= h < 14: return "Chào buổi trưa"
    if 14 <= h < 18: return "Chào buổi chiều"
    return "Chào buổi tối"

def _tech_on() -> bool:
    return bool(st.session_state.get("tech_mode", False))

def _render_approval_card(msg: dict, idx: int, session_id: str, employee_id: str) -> None:
    pending = msg.get("pending_action") or {}
    ptype = pending.get("type", "hành động")
    params = pending.get("params", {}) or {}
    params_html = " · ".join(f"**{k}**: `{v}`" for k, v in params.items())
    st.markdown(approval_card(f"MAIA muốn làm giúp bạn: <b>{ptype}</b><br>{pending.get('summary', '')}", params_html), unsafe_allow_html=True)
    resolved = st.session_state.get(f"resolved_{idx}", False)
    is_latest = idx == st.session_state.get("last_approval_idx")
    if not resolved and is_latest:
        c1, c2 = st.columns(2)
        if c1.button("✅ Đồng ý, làm giúp tôi", key=f"approve_{idx}", use_container_width=True, type="primary"):
            with st.spinner("MAIA đang thực hiện..."):
                if st.session_state.get("langgraph_mode"):
                    out = _langgraph_resume(session_id, employee_id, approved=True)
                else:
                    from maia.agent.agent import EnterpriseAgent
                    out = EnterpriseAgent().confirm_action(session_id, employee_id=employee_id, approved=True)
            st.session_state[f"resolved_{idx}"] = True
            st.session_state.messages.append(_to_msg(out))
            _persist_active()
            st.rerun()
        if c2.button("◻️ Thôi, hủy nhé", key=f"cancel_{idx}", use_container_width=True):
            with st.spinner("Đang hủy..."):
                if st.session_state.get("langgraph_mode"):
                    out = _langgraph_resume(session_id, employee_id, approved=False)
                else:
                    from maia.agent.agent import EnterpriseAgent
                    out = EnterpriseAgent().confirm_action(session_id, employee_id=employee_id, approved=False)
            st.session_state[f"resolved_{idx}"] = True
            st.session_state.messages.append(_to_msg(out))
            _persist_active()
            st.rerun()
    elif resolved:
        st.caption("Đã xử lý rồi")

def _render_action_completed(msg: dict) -> None:
    action = msg.get("action") or {}
    result = action.get("result", {}) or {}
    big = result.get("ticket_id") or result.get("request_id") or "Xong rồi"
    rows = {k: v for k, v in result.items() if k in ("status", "assignee", "days", "start_date", "remaining_balance")}
    rows_html = " · ".join(f"<b>{k}</b>: <code>{v}</code>" for k, v in rows.items())
    st.success("Đã xong giúp bạn rồi đây")
    st.markdown(action_completed_big_id(big, rows_html), unsafe_allow_html=True)

def _render_insufficient(msg: dict, idx: int) -> None:
    retrieved = msg.get("retrieved") or []
    st.markdown(no_evidence_card(msg.get("content", ""), len(retrieved)), unsafe_allow_html=True)
    a1, a2, a3 = st.columns(3)
    if a1.button("🔁 Tìm lại", key=f"retry_{idx}", use_container_width=True):
        st.session_state["retry_q"] = st.session_state.get("last_user_q", "")
        st.rerun()
    if retrieved and a2.button("📂 Xem đoạn đã đọc", key=f"seen_{idx}", use_container_width=True):
        st.session_state[f"show_retrieved_{idx}"] = not st.session_state.get(f"show_retrieved_{idx}", False)
        st.rerun()
    with a3.popover("📞 Liên hệ hỗ trợ", use_container_width=True):
        st.markdown(contacts_md())
    st.markdown(noev_tip(), unsafe_allow_html=True)
    if retrieved and st.session_state.get(f"show_retrieved_{idx}", False):
        st.caption(f"Đoạn tài liệu đã đối chiếu ({len(retrieved)}) — chưa đủ chắc:")
        for j, r in enumerate(retrieved, 1):
            meta = f"{r.get('tag', '')} {r.get('filename', '')}".strip() or f"Đoạn {j}"
            with st.expander(f"📄 {meta}"):
                st.write(r.get("text") or r.get("content") or "(không có nội dung)")

def _render_evidence_badge(msg: dict, kind: str = "ok", msg_idx: int = 0) -> None:
    n = len(msg.get("citations") or [])
    icon = "✓" if kind == "ok" else "⚠"
    label = f"Có căn cứ · {n} nguồn" if kind == "ok" else "Chưa tìm thấy căn cứ"
    with st.popover(f"{icon} {label} ⌄", help="Xem nguồn tham chiếu"):
        st.markdown(f"**Nguồn tham khảo**{f' · {n}' if n else ''}")
        if n:
            for i, c in enumerate(msg["citations"], 1):
                section = c.get("section") or c.get("page") or ""
                rel = c.get("relevance", "low")
                rel_label, _, _ = REL_VI.get(rel, REL_VI["low"])
                st.markdown(evidence_source_row(i, c.get("filename", "?"), c.get("tag", ""), rel_label, section), unsafe_allow_html=True)
                with st.expander(f"Xem đoạn AI đã đọc ({c.get('tag', f'[S{i}]')})"):
                    from maia.source_reader import render_excerpt_html
                    st.markdown(render_excerpt_html(c.get("text", ""), c.get("tag", ""), c.get("filename", "")), unsafe_allow_html=True)
                    _render_full_source(msg_idx, i, c)
        else:
            st.caption("MAIA chưa dùng nguồn nào cho câu trả lời này.")


@st.cache_data(ttl=60, show_spinner=False)
def _cached_source_view(chunk_id: str, filename: str, excerpt: str, tenant_id: str) -> dict:
    from maia.source_reader import fetch_source_view
    return fetch_source_view(chunk_id, filename, excerpt, tenant_id=tenant_id)


def _render_full_source(msg_idx: int, i: int, c: dict) -> None:
    """Lazy full-document reader: excerpt highlight stays on top while reading."""
    from maia.source_reader import render_excerpt_html, render_source_html
    key = f"fullsrc_{msg_idx}_{i}_{c.get('chunk_id', '')}"
    opened = bool(st.session_state.get(key, False))
    if st.button("📖 Đọc toàn văn" if not opened else "▴ Thu gọn toàn văn", key=f"btn_{key}"):
        st.session_state[key] = not opened
        st.rerun()
    if not st.session_state.get(key, False):
        return
    tenant_id = st.session_state.get("tenant_id", settings.TENANT_ID)
    with st.spinner("Đang tải toàn văn nguồn…"):
        try:
            view = _cached_source_view(c.get("chunk_id", ""), c.get("filename", ""), c.get("text", ""), tenant_id)
        except Exception as e:
            st.error(f"Không tải được toàn văn: {e}")
            return
    st.caption(f"**{view.get('filename', '?')}** · {view.get('total_chunks', 1)} đoạn trong tài liệu"
               + (" · (đã rút gọn)" if view.get("truncated") else ""))
    # Khoanh vùng vẫn giữ phía trên để đối chiếu khi đọc toàn văn.
    st.markdown(render_excerpt_html(view.get("cited_text", c.get("text", "")), c.get("tag", ""), view.get("filename", "")), unsafe_allow_html=True)
    st.markdown(render_source_html(view), unsafe_allow_html=True)

def _render_assistant(msg: dict, idx: int, session_id: str, employee_id: str) -> None:
    status = msg.get("status", "answered")
    with st.chat_message("assistant", avatar="🌿"):
        st.markdown(status_pill(status), unsafe_allow_html=True)
        if status == "insufficient_evidence":
            _render_insufficient(msg, idx)
        elif status == "needs_approval":
            st.markdown(msg.get("content", ""))
            _render_approval_card(msg, idx, session_id, employee_id)
        elif status == "action_completed":
            st.markdown(msg.get("content", ""))
            _render_action_completed(msg)
        elif status == "action_cancelled":
            st.info(msg.get("content", "") or "Đã hủy theo ý bạn nhé")
        elif status == "needs_clarification":
            st.markdown("💬 " + (msg.get("content", "") or "Bạn nói rõ thêm giúp MAIA một chút nhé?"))
        elif status == "error":
            st.error("Úi, có chút trục trặc " + (msg.get("content", "") or "Bạn thử lại sau giây lát."))
        else:
            st.markdown(msg.get("content", ""))
            if msg.get("citations"):
                _render_evidence_badge(msg, "ok", msg_idx=idx)
                st.markdown(f"<div class='maia-cap' style='margin-top:6px'>{sources_line_compact(msg.get('citations') or [])}</div>", unsafe_allow_html=True)

def _to_msg(res: dict) -> dict:
    return {"role": "assistant", "content": res.get("answer", ""), "status": res.get("status", "answered"), "intent": res.get("intent", "general"), "citations": res.get("citations", []), "retrieved": res.get("retrieved", []), "evidence": res.get("evidence", {}), "grounding": res.get("grounding", {}), "action": res.get("action"), "pending_action": res.get("pending_action"), "slots": res.get("slots", {})}

# ---------------------------------------------------------------- sidebar + menu
def _day_group(ts: float) -> str:
    d = datetime.date.fromtimestamp(ts)
    today = datetime.date.today()
    if d == today: return "Hôm nay"
    if d == today - datetime.timedelta(days=1): return "Hôm qua"
    return d.strftime("%d/%m")

with st.sidebar:
    st.markdown(sidebar_header(len(st.session_state.chat_sessions)), unsafe_allow_html=True)
    if st.button("＋ Đoạn chat mới", use_container_width=True, type="primary"):
        _new_chat()
        st.rerun()
    q = st.text_input("⌕ Tìm cuộc trò chuyện", value="", placeholder="Tìm...", label_visibility="collapsed")
    sessions = sorted(st.session_state.chat_sessions.items(), key=lambda kv: kv[1].get("updated", 0), reverse=True)
    if q.strip():
        ql = q.strip().lower()
        sessions = [(k, s) for k, s in sessions if ql in (s.get("title", "") or "").lower() or any(ql in (m.get("content", "") or "").lower() for m in s.get("messages", [])[-6:])]
    if sessions:
        cur_group = None
        for key, s in sessions:
            group = _day_group(s.get("updated", 0))
            if group != cur_group:
                cur_group = group
                st.markdown(sidebar_group_label(group), unsafe_allow_html=True)
            is_active = key == st.session_state.active_key
            b1, b2 = st.columns([6, 1])
            if b1.button(("📌 " if is_active else "") + (s.get("title", "Đoạn chat mới") or "Đoạn chat mới"), key=f"open_{key}", type="tertiary" if not is_active else "primary", use_container_width=True, disabled=is_active, help=f"Mở phiên · {_day_group(s.get('updated', 0))}"):
                _switch_chat(key)
                st.rerun()
            if b2.button("🗑", key=f"del_{key}", type="tertiary", help="Xóa phiên"):
                _delete_chat(key)
                st.rerun()
    else:
        st.caption("Chưa có phiên nào khớp.")
    st.markdown(sidebar_footer(st.session_state.employee_id, len(st.session_state.chat_sessions)), unsafe_allow_html=True)
    if st.button("🧹 Xóa nội dung phiên đang mở", type="tertiary", use_container_width=True):
        from maia.agent.session import session_store
        try:
            session_store.clear(backend_session_id)
        except Exception:
            pass
        s = st.session_state.chat_sessions[st.session_state.active_key]
        s["messages"] = []
        s["title"] = "Đoạn chat mới"
        for k in [k for k in list(st.session_state.keys()) if isinstance(k, str) and (k.startswith("resolved_") or k in ("messages", "last_approval_idx", "last_user_q", "retry_q"))]:
            st.session_state.pop(k, None)
        st.session_state.messages = []
        _persist_active()
        st.rerun()

# ---------------------------------------------------------------- top bar + menu
# Single status pill (no duplicate): neutral for end users, backend detail
# only when tech mode is on.
_is_local = "localhost" in settings.QDRANT_URL
_show_tech = bool(st.session_state.get("tech_mode", False) or settings.UI_SHOW_TECH_BADGE)
top_l, top_r = st.columns([7, 3])
with top_l:
    st.markdown(topbar_html(), unsafe_allow_html=True)
with top_r:
    dot_col, menu_col = st.columns([1.4, 1])
    with dot_col:
        st.markdown(status_dot_html(is_local=_is_local, show_tech=_show_tech), unsafe_allow_html=True)
    with menu_col:
        with st.popover("☰", help="Menu"):
            st.markdown("### MAIA · Menu")
            _nm = _auth_user.get("full_name") or _auth_user.get("email", "")
            st.markdown(f"👤 **{_nm}**  \n`{_auth_user.get('email', '')}` · "
                        f"mã NV `{st.session_state.employee_id}` · "
                        f"vai trò `{'ADMIN' if _is_admin else 'user'}`")
            st.checkbox("🌙 Chế độ tối", key="dark_mode")
            if st.button("Đăng xuất", use_container_width=True):
                _logout()
                st.rerun()
            tenant_id = st.session_state.tenant_id
            with st.expander("Cài đặt nâng cao"):
                st.checkbox("Chế độ kỹ thuật", key="tech_mode")
                st.checkbox("🤖 LangGraph Agent (thử nghiệm)", key="langgraph_mode",
                            help="Dùng LangGraph control-plane agent (/agent/chat) thay vì EnterpriseAgent cũ. Hỗ trợ SSE streaming + phê duyệt HITL qua API.")
                st.markdown("#### Điều chỉnh sinh câu trả lời")
                st.session_state.retr_top_k = st.slider("Số nguồn tham khảo", 1, 5, min(st.session_state.get("retr_top_k", settings.TOP_K_FINAL), 5))
                if _tech_on():
                    tc1, tc2 = st.columns(2)
                    st.session_state.gen_top_k = tc1.slider("Top K", 1, 100, st.session_state.get("gen_top_k", 50))
                    st.session_state.gen_top_p = tc2.slider("Top P", 0.05, 1.0, st.session_state.get("gen_top_p", 1.0), 0.05)
                    tc3, tc4 = st.columns(2)
                    st.session_state.gen_temperature = tc3.slider("Temperature", 0.0, 1.0, st.session_state.get("gen_temperature", 0.1), 0.05)
                    st.session_state.gen_max_tokens = tc4.slider("Max tokens", 256, 2048, st.session_state.get("gen_max_tokens", 512), 64)
                else:
                    st.session_state.setdefault("gen_top_k", 50)
                    st.session_state.setdefault("gen_top_p", 1.0)
                    st.session_state.setdefault("gen_temperature", 0.1)
                    st.session_state.setdefault("gen_max_tokens", 512)
                st.markdown("#### Hệ thống")
                if _tech_on():
                    if "localhost" in settings.QDRANT_URL:
                        st.warning("Đang dùng Qdrant local (chỉ hiện ở chế độ kỹ thuật)")
                    try:
                        _, store, _, reranker, llm = build_stack()
                        ok = True
                    except Exception as e:
                        ok = False
                        st.error(f"Backend chưa sẵn sàng: {e}")
                    if ok:
                        dot = "🟢" if llm.mode != "error" else "🔴"
                        st.markdown(f"{dot} **{'Sẵn sàng' if llm.mode != 'error' else 'Cần kiểm tra'}** · `{store.count()} đoạn tri thức`")
                else:
                    st.caption("🟢 MAIA đã sẵn sàng. Bật Chế độ kỹ thuật để xem chi tiết backend.")
                uploaded = st.file_uploader("Kéo file vào đây (.md / .txt / .pdf)", accept_multiple_files=True)
                if uploaded and st.button("Nạp file vừa chọn", use_container_width=True, type="primary"):
                    dest = Path(settings.DATA_DIR)
                    dest.mkdir(parents=True, exist_ok=True)
                    for f in uploaded:
                        (dest / f.name).write_bytes(f.read())
                    with st.spinner("Đang đọc file…"):
                        st.json(ingest_data_dir(str(dest)))
                    st.toast(f"Đã nạp {len(uploaded)} file")
            st.divider()
            st.markdown("#### Link tham khảo (phiên này)")
            new_url = st.text_input("Dán link", value="", key="url_input", placeholder="https://...", label_visibility="collapsed")
            if st.button("Thêm link", use_container_width=True):
                if not (new_url or "").strip():
                    st.warning("Bạn dán link trước")
                else:
                    from maia.ingestion_pipeline import ingest_url
                    with st.spinner("Đang tải + đọc link…"):
                        try:
                            res = ingest_url(new_url.strip(), tenant_id=tenant_id, session_id=backend_session_id)
                            st.success(f"Đã thêm: {res['title']} ({res['chunks']} đoạn)")
                        except ValueError as e:
                            st.error(f"Thêm link thất bại: {e}")
            try:
                from maia.ingestion_pipeline import delete_source, list_sources
                _srcs = list_sources(backend_session_id, tenant_id)
            except Exception as e:
                _srcs = []
                st.error(f"Backend chưa sẵn sàng: {e}")
            if _srcs:
                st.caption(f"{len(_srcs)} link trong phiên này:")
                for s in _srcs:
                    cc1, cc2 = st.columns([5, 1])
                    cc1.markdown(f"[{s['title']}]({s['url']})  `{s['chunks']} đoạn`")
                    if cc2.button("🗑", key=f"del_src_{s['doc_id']}", help="Xoa"):
                        delete_source(s["doc_id"], backend_session_id, tenant_id)
                        st.rerun()
            else:
                st.caption("Chưa có link nào.")

if "employee_id" not in locals():
    employee_id = st.session_state.employee_id
if "tenant_id" not in locals():
    tenant_id = st.session_state.tenant_id
session_id = backend_session_id

# ---------------------------------------------------------------- admin dashboard
def _dash_get(path: str, params: dict | None = None):
    try:
        r = _api("GET", path, st.session_state.get("auth_token", ""), params=params or {})
        if r.status_code == 200:
            return r.json()
        st.error(f"Không tải được {path} (mã {r.status_code})")
    except Exception as e:
        st.error(f"Không kết nối được API: {e}")
    return None


def _dash_post(path: str, payload: dict | None = None, method: str = "POST"):
    try:
        r = _api(method, path, st.session_state.get("auth_token", ""), json=payload or {})
        if r.status_code in (200, 201):
            return r.json()
        try:
            st.error(r.json().get("detail", f"Lỗi {r.status_code}"))
        except Exception:
            st.error(f"Lỗi {r.status_code}")
    except Exception as e:
        st.error(f"Không kết nối được API: {e}")
    return None


def _fmt_ts(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts or 0)).strftime("%d/%m %H:%M")
    except Exception:
        return "—"


def _render_dashboard() -> None:
    st.markdown("## 💼 Dashboard quản trị")
    st.caption("Quản lý người dùng, duyệt yêu cầu nghỉ phép / IT ticket, theo dõi email thông báo.")
    stats = _dash_get("/admin/stats") or {}
    wf = stats.get("workflow", {}) or {}
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Người dùng", stats.get("total_users", "—"))
    k2.metric("Đang hoạt động", stats.get("active_users", "—"))
    k3.metric("Yêu cầu chờ duyệt", wf.get("pending", "—"))
    k4.metric("Đã duyệt", wf.get("approved", "—"))
    k5.metric("Đoạn tri thức", stats.get("kb_points", "—"))
    st.caption(f"LLM: `{stats.get('llm_mode', '?')}` · Admin: {stats.get('admin_users', '—')}")

    tab_req, tab_users, tab_act, tab_mail, tab_docs = st.tabs(
        ["⏳ Chờ duyệt", "👥 Người dùng", "📜 Hoạt động", "✉️ Email", "📚 Tài liệu"])

    with tab_req:
        f1, f2 = st.columns(2)
        f_status = f1.selectbox("Trạng thái", ["pending", "approved", "rejected", "all"], index=0)
        f_type = f2.selectbox("Loại", ["all", "create_leave_request", "create_it_ticket"], index=0)
        data = _dash_get("/admin/requests", {"status": "" if f_status == "all" else f_status,
                                             "type": "" if f_type == "all" else f_type}) or {}
        reqs = data.get("requests", [])
        if not reqs:
            st.info("Không có yêu cầu nào.")
        for r in reqs:
            with st.container(border=True):
                st.markdown(f"**#{r['id']} · {r.get('summary', '')}**")
                st.caption(f"{r.get('type', '')} · {r.get('requester_email', '')} "
                           f"({r.get('employee_id', '')}) · {_fmt_ts(r.get('created_at'))} "
                           f"· trạng thái `{r.get('status', '')}`")
                if r.get("params"):
                    st.json(r["params"])
                if r.get("status") == "pending":
                    b1, b2 = st.columns(2)
                    if b1.button("✅ Duyệt", key=f"ap_{r['id']}", type="primary", use_container_width=True):
                        out = _dash_post(f"/admin/requests/{r['id']}/decide", {"approved": True})
                        if out:
                            st.success("Đã duyệt" + ("" if out.get("executed") else " (ghi nhận + đã báo email)"))
                            st.session_state["last_approved_id"] = r["id"]
                            st.rerun()
                    if b2.button("❌ Từ chối", key=f"rj_{r['id']}", use_container_width=True):
                        out = _dash_post(f"/admin/requests/{r['id']}/decide", {"approved": False})
                        if out:
                            st.success("Đã từ chối + báo email.")
                            st.rerun()
                elif r.get("result_ref"):
                    st.caption(f"Mã tham chiếu: `{r['result_ref']}` · người duyệt: {r.get('decided_by', '')}")

    if st.session_state.get("last_approved_id"):
        st.info(f"✅ Đã duyệt yêu cầu #{st.session_state['last_approved_id']}. Chuyển sang tab '👥 Người dùng' hoặc '📜 Hoạt động' để xem chi tiết.")

    with tab_users:
        q = st.text_input("Tìm theo email/tên/mã NV", key="adm_q", placeholder="Tìm...")
        users = (_dash_get("/admin/users", {"skip": 0, "limit": 200}) or [])
        if q.strip():
            ql = q.strip().lower()
            users = [u for u in users if ql in (u.get("email", "") or "").lower()
                     or ql in (u.get("full_name", "") or "").lower()
                     or ql in (u.get("employee_id", "") or "").lower()]
        if users:
            st.dataframe([{"Email": u.get("email"), "Họ tên": u.get("full_name", ""),
                           "Mã NV": u.get("employee_id"), "Phòng": u.get("department", ""),
                           "Vai trò": u.get("role"), "Kích hoạt": u.get("is_active"),
                           "ID": str(u.get("id"))} for u in users],
                         use_container_width=True, hide_index=True)
        else:
            st.info("Chưa có người dùng nào.")
        with st.expander("➕ Tạo người dùng"):
            n1, n2 = st.columns(2)
            nu_email = n1.text_input("Email", key="nu_email")
            nu_pw = n2.text_input("Mật khẩu (≥6 ký tự)", type="password", key="nu_pw")
            n3, n4, n5 = st.columns(3)
            nu_name = n3.text_input("Họ tên", key="nu_name")
            nu_dept = n4.text_input("Phòng ban", key="nu_dept")
            nu_role = n5.selectbox("Vai trò", ["user", "admin"], key="nu_role")
            if st.button("Tạo", type="primary", key="nu_create"):
                out = _dash_post("/admin/users", {"email": nu_email.strip(), "password": nu_pw,
                                                 "full_name": nu_name.strip(),
                                                 "department": nu_dept.strip(), "role": nu_role})
                if out:
                    st.success(f"Đã tạo {out.get('email')} ({out.get('employee_id')})")
                    st.rerun()
        if users:
            with st.expander("⚙️ Quản lý tài khoản"):
                sel = st.selectbox("Chọn tài khoản", [u.get("email") for u in users], key="adm_sel")
                u = next((x for x in users if x.get("email") == sel), None)
                if u:
                    st.caption(f"Mã NV `{u.get('employee_id')}` · vai trò `{u.get('role')}` · "
                               f"{'đang hoạt động' if u.get('is_active') else 'đang bị khóa'}")
                    c1, c2, c3 = st.columns(3)
                    if c1.button("Chuyển vai trò", key="adm_role"):
                        new_role = "user" if u.get("role") == "admin" else "admin"
                        out = _dash_post(f"/admin/users/{u['id']}", {"email": u["email"],
                                                                     "role": new_role,
                                                                     "tenant_id": u.get("tenant_id", "default"),
                                                                     "password": ""}, method="PUT")
                        if out:
                            st.success(f"Đã chuyển thành {new_role}")
                            st.rerun()
                    if c2.button("Khóa" if u.get("is_active") else "Mở khóa", key="adm_lock"):
                        out = _dash_post(f"/admin/users/{u['id']}/active",
                                         {"is_active": not u.get("is_active")})
                        if out:
                            st.success("Đã cập nhật trạng thái.")
                            st.rerun()
                    npw = c3.text_input("Mật khẩu mới", type="password", key="adm_npw")
                    if c3.button("Đặt lại MK", key="adm_pw"):
                        out = _dash_post(f"/admin/users/{u['id']}", {"email": u["email"],
                                                                     "role": u.get("role"),
                                                                     "tenant_id": u.get("tenant_id", "default"),
                                                                     "password": npw}, method="PUT")
                        if out:
                            st.success("Đã đặt lại mật khẩu.")
                            st.rerun()

    with tab_act:
        ev = (_dash_get("/admin/activity", {"limit": 50}) or {}).get("events", [])
        if ev:
            st.dataframe([{"Thời gian": _fmt_ts(e.get("ts")), "Sự kiện": e.get("kind"),
                           "Yêu cầu": e.get("summary", "") or f"#{e.get('request_id')}",
                           "Người thực hiện": e.get("actor", ""),
                           "Trạng thái": e.get("status", "")} for e in ev],
                         use_container_width=True, hide_index=True)
        else:
            st.info("Chưa có hoạt động nào.")

    with tab_mail:
        box = (_dash_get("/admin/outbox", {"limit": 100}) or {})
        mails = box.get("emails", [])
        if box.get("smtp_configured"):
            st.success("SMTP đã cấu hình — email gửi thật.")
        else:
            st.warning("Chưa cấu hình SMTP — email đang xếp hàng ở Outbox (file). Cấu hình SMTP_HOST/PORT/USERNAME/PASSWORD trong .env để gửi thật.")
        if mails:
            st.dataframe([{"Thời gian": (m.get("ts", "") or "")[:16].replace("T", " "),
                           "Tới": m.get("to"), "Tiêu đề": m.get("subject"),
                           "Kênh": m.get("via", ""), "Loại": m.get("kind", "")} for m in mails],
                         use_container_width=True, hide_index=True)
            sel_m = st.selectbox("Xem nội dung email", [f"#{n} {m.get('subject', '')}" for n, m in enumerate(mails)], key="mail_sel")
            try:
                idx = int(sel_m.split(" ")[0][1:])
                st.text(mails[idx].get("body", ""))
            except Exception:
                pass
        else:
            st.info("Outbox trống.")

    with tab_docs:
        docs = (_dash_get("/admin/documents", {"skip": 0, "limit": 100}) or {})
        items = docs.get("documents", [])
        st.caption(f"Tổng {docs.get('total_docs', 0)} tài liệu trong kho tri thức.")
        if items:
            st.dataframe([{"Tài liệu": d.get("filename") or d.get("doc_id"),
                           "Số đoạn": d.get("chunks"),
                           "Tenant": ", ".join(d.get("tenant_ids", []))} for d in items],
                         use_container_width=True, hide_index=True)


if _is_admin:
    with st.sidebar:
        st.radio("Chế độ xem", ["💬 Chat", "💼 Dashboard"], key="dash_view",
                 horizontal=True, label_visibility="collapsed")
    if st.session_state.get("dash_view") == "💼 Dashboard":
        _render_dashboard()
        st.markdown(footer_html(), unsafe_allow_html=True)
        st.stop()


# ---------------------------------------------------------------- empty state + chat
msgs = st.session_state.get("messages", [])
demo_q = None
if not msgs:
    st.markdown(hero_section(_greeting()), unsafe_allow_html=True)
    with st.container(key="maia_sugrow"):
        cols = st.columns(4)
        for n, (col, (icon, label, sub, question)) in enumerate(zip(cols, SUGGESTIONS)):
            if col.button(f"{icon} {label}", key=f"sug_{n}_{label}", use_container_width=True, help=sub):
                demo_q = question
else:
    # Follow-up chips above the composer (GPT/Gemini-style).
    with st.container(key="maia_sugrow"):
        cols = st.columns(3)
        for n, (col, (icon, label, question)) in enumerate(zip(cols, FOLLOWUPS)):
            if col.button(f"{icon} {label}", key=f"fol_{n}_{label}", use_container_width=True):
                demo_q = question

for i, m in enumerate(st.session_state.messages):
    if m.get("role") == "user":
        with st.chat_message("user", avatar="🧑‍💼"):
            st.markdown(user_bubble(m.get("content", "")), unsafe_allow_html=True)
    else:
        _render_assistant(m, i, session_id, employee_id)

def _ask(prompt: str, session_id: str, employee_id: str, tenant_id: str) -> None:
    from maia.agent.agent import EnterpriseAgent
    gen = {"temperature": st.session_state.get("gen_temperature", 0.1), "top_p": st.session_state.get("gen_top_p", 1.0), "top_k": int(st.session_state.get("gen_top_k", 50)), "max_tokens": int(st.session_state.get("gen_max_tokens", 512))}
    top_k_final = st.session_state.get("retr_top_k") or None
    requester_email = st.session_state.get("auth_email", "")
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state["last_user_q"] = prompt
    _persist_active()
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(user_bubble(prompt), unsafe_allow_html=True)
    with st.status("MAIA đang tìm trong kho tri thức…", expanded=True) as status:
        st.write("Đang tìm…")
        st.write("Đang đối chiếu nguồn…")
        res = EnterpriseAgent().chat(prompt, session_id=session_id, employee_id=employee_id, tenant_id=tenant_id, top_k_final=top_k_final, gen=gen, requester_email=requester_email)
        st.write("Đang tạo câu trả lời…")
        status.update(label="Đã xong", state="complete", expanded=False)
    msg = _to_msg(res)
    st.session_state.messages.append(msg)
    if msg["status"] == "needs_approval":
        st.session_state["last_approval_idx"] = len(st.session_state.messages) - 1
    _persist_active()
    _render_assistant(msg, len(st.session_state.messages) - 1, session_id, employee_id)

def _langgraph_resume(session_id: str, employee_id: str, approved: bool) -> dict:
    """Resume a LangGraph interrupted run via the /agent/chat API (HITL)."""
    try:
        r = _api("POST", "/agent/chat", st.session_state.get("auth_token"),
                 json={"question": "", "session_id": session_id,
                       "resume": {"approved": approved, "employee_id": employee_id}})
        return r.json() if r.status_code == 200 else {"status": "error", "answer": f"API error {r.status_code}"}
    except Exception as e:
        return {"status": "error", "answer": str(e)}


def _ask_langgraph(prompt: str, session_id: str, employee_id: str, tenant_id: str) -> None:
    """Chat via the LangGraph /agent/chat API endpoint (HITL + streaming)."""
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state["last_user_q"] = prompt
    _persist_active()
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(user_bubble(prompt), unsafe_allow_html=True)
    with st.status("MAIA (LangGraph) đang xử lý…", expanded=True) as status:
        st.write("Đang phân tích & tìm kiếm…")
        try:
            import json as _json
            r = _api("POST", "/agent/chat", st.session_state.get("auth_token"),
                     json={"question": prompt, "session_id": session_id, "stream": False})
            res = r.json() if r.status_code == 200 else {"status": "error", "answer": f"API error {r.status_code}"}
        except Exception as e:
            res = {"status": "error", "answer": str(e)}
        st.write("Đã nhận kết quả")
        status.update(label="Đã xong", state="complete", expanded=False)
    msg = _to_msg(res)
    # Map LangGraph-specific keys into the unified message shape.
    if not msg.get("pending_action") and res.get("pending_action"):
        msg["pending_action"] = res["pending_action"]
    if msg["status"] == "needs_approval" and not msg.get("pending_action"):
        msg["pending_action"] = res.get("pending_action")
    st.session_state.messages.append(msg)
    if msg["status"] == "needs_approval":
        st.session_state["last_approval_idx"] = len(st.session_state.messages) - 1
    _persist_active()
    _render_assistant(msg, len(st.session_state.messages) - 1, session_id, employee_id)


prompt = st.chat_input("Hỏi MAIA về chính sách, IT, bảo mật, VPN…")
if "demo_q" not in locals():
    demo_q = None
if demo_q:
    prompt = demo_q
retry_q = st.session_state.pop("retry_q", None)
if retry_q:
    prompt = retry_q

if prompt:
    if st.session_state.get("langgraph_mode"):
        _ask_langgraph(prompt, session_id, employee_id, tenant_id)
    else:
        _ask(prompt, session_id, employee_id, tenant_id)
    st.rerun()

st.divider()
if _tech_on():
    with st.expander("Chế độ tra cứu nhanh (1 lượt, không hội thoại)"):
        q = st.text_input("Câu hỏi:", "MAIA RAG pipeline gồm những bước nào?")
        top_k = st.slider("Số nguồn lấy về (debug)", 1, 8, settings.TOP_K_FINAL)
        if st.button("Tìm nhanh"):
            with st.spinner("Đang tìm…"):
                from maia.pipeline_query import query
                res = query(q, top_k_final=top_k)
            st.write(res["answer"])
            for c in res.get("citations", []):
                with st.expander(f"{c['tag']} {c['filename']}"):
                    st.write(c["text"])

st.markdown(footer_html(), unsafe_allow_html=True)

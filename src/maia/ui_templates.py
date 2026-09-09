"""HTML template functions for MAIA UI.

All HTML rendering is centralized here so app_streamlit.py stays clean
and we don't break Python syntax when editing HTML.
"""


def user_bubble(text: str) -> str:
    """Render a user chat message bubble."""
    safe = (text or "").strip() or "(trống)"
    safe = safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return f"<div class='maia-user-bubble'>{safe}</div>"


def quote_block(text: str) -> str:
    """Render a quoted source excerpt with markdown-safe escaping."""
    safe = (text or "").strip() or "(trống)"
    safe = safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe = safe.replace("#", "&#35;").replace("@", "&#64;")
    safe = safe.replace("\n", "<br>")
    return f"<div class='maia-quote'>{safe}</div>"


def hero_section(greeting: str) -> str:
    """Render the compact empty-state welcome (only shown before the first message)."""
    return (
        "<div class='maia-empty'>"
        "<div class='maia-empty-brand'><span class='maia-mark'>M</span>"
        "<h1>MAIA</h1></div>"
        "<p class='maia-empty-tag'>Trợ lý tri thức nội bộ</p>"
        "<p class='maia-empty-sub'>Tra cứu chính sách, IT, bảo mật và quy trình — "
        "mọi câu trả lời đều kèm nguồn trích dẫn rõ ràng.</p>"
        "<div class='maia-capab'>"
        "<span><b>&#10003;</b> Trích dẫn nguồn</span>"
        "<span><b>&#10003;</b> Trung thực khi thiếu căn cứ</span>"
        "</div>"
        "</div>"
    )

def topbar_html(brand: str = "MAIA", tagline: str = "Trợ lý tri thức nội bộ",
                sysdot_label: str = "Sẵn sàng", is_local: bool = False,
                show_tech: bool = False) -> str:
    """Render the top navigation bar brand block (no status pill inside).

    The status pill is rendered ONCE by the caller (app_streamlit top-right)
    to avoid the duplicate-pill overlap. Backend details (e.g. 'Qdrant local')
    appear only when show_tech=True.
    """
    return (
        "<div class='maia-topbar'>"
        "<div class='maia-brand'><span class='maia-mark'></span>"
        f"<span class='maia-brand-name'>{brand}<small>{tagline}</small></span>"
        "</div>"
        "</div>"
    )


def status_dot_html(is_local: bool = False, show_tech: bool = False) -> str:
    """Single status pill for the top-right corner (call once per page)."""
    label = "Qdrant local" if (is_local and show_tech) else "Sẵn sàng"
    local_cls = " local" if (is_local and show_tech) else ""
    return (
        "<div class='maia-topstatus' style='justify-content:flex-end'>"
        f"<span class='maia-sysdot{local_cls}' aria-label='Trạng thái hệ thống'>"
        f"<i></i>{label}</span></div>"
    )


def sidebar_header(session_count: int) -> str:
    """Render the sidebar header with session count."""
    return (
        f"<div class='maia-side-head'><h3>Phiên trò chuyện</h3>"
        f"<span>{session_count} phiên</span></div>"
    )


def sidebar_group_label(group: str) -> str:
    """Render a date-group label in the sidebar."""
    return f"<div class='maia-side-group'>{group}</div>"


def sidebar_session_item(title: str, group: str, is_active: bool) -> str:
    """Render one conversation row in the sidebar."""
    active_cls = " active" if is_active else ""
    return (
        f"<div class='maia-session{active_cls}'>"
        f"<div class='maia-session-title'>{title}</div>"
        f"<div class='maia-session-sub'>{group}</div>"
        "</div>"
    )


def sidebar_footer(employee_id: str, session_count: int) -> str:
    """Render the fixed sidebar footer."""
    return (
        "<div class='maia-side-foot'></div>"
        f"<div style='margin-top:6px;font-size:11.5px;color:var(--maia-mute)'>"
        f"👤 {employee_id} · {session_count} phiên</div>"
    )


def evidence_source_row(index: int, filename: str, tag: str,
                       relevance_label: str, section: str = "") -> str:
    """Render one source row inside the evidence drawer."""
    section_part = f" · {section}" if section else ""
    return (
        "<div class='maia-src-row'><span class='maia-src-num'>"
        f"{index}</span><div><div class='maia-src-name'>{filename}</div>"
        f"<div class='maia-src-path'>{tag} · {relevance_label}{section_part}</div></div></div>"
    )


def evidence_caption() -> str:
    """Caption shown above source text to clarify it's a chunk."""
    return "Trích đoạn tài liệu (chunk) — không phải toàn bộ document"


def status_pill(status: str) -> str:
    """Map internal status → Vietnamese pill HTML (short, user-facing)."""
    mapping = {
        "answered": "<span class='maia-pill pill-ok'>✓ Có căn cứ</span>",
        "insufficient_evidence": "<span class='maia-pill pill-warn'>⚠ Không có căn cứ nội bộ</span>",
        "needs_approval": "<span class='maia-pill pill-info'>● Cần bạn duyệt</span>",
        "action_completed": "<span class='maia-pill pill-ok'>✓ Đã hoàn thành</span>",
        "action_cancelled": "<span class='maia-pill pill-mute'>● Đã hủy</span>",
        "needs_clarification": "<span class='maia-pill pill-warn'>● Cần thêm thông tin</span>",
        "error": "<span class='maia-pill pill-bad'>⚠ Gặp sự cố</span>",
    }
    return mapping.get(status, mapping["answered"])


def sources_line_compact(citations: list[dict], max_names: int = 3) -> str:
    """One-line compact sources footer (ChatGPT-style), no chunk_ids."""
    names: list[str] = []
    for c in citations or []:
        fn = (c.get("filename") or "?").strip()
        if fn and fn not in names:
            names.append(fn)
        if len(names) >= max_names:
            break
    if not names:
        return ""
    extra = f" +{len(citations) - len(names)}" if len(citations) > len(names) else ""
    return "📎 " + " · ".join(names) + extra


def no_evidence_card(content: str, retrieved_count: int) -> str:
    """Render the 'insufficient evidence' card."""
    note = (f" Đã đối chiếu {retrieved_count} đoạn tài liệu nhưng chưa đoạn nào đủ chắc."
             if retrieved_count else "")
    return (
        "<div class='maia-noev'>"
        "<div class='maia-noev-title'>⚠ Không tìm thấy căn cứ nội bộ</div>"
        f"<p>{content or 'MAIA chưa tìm thấy thông tin phù hợp trong kho tri thức hiện có.'}</p>"
        f"<div class='maia-noev-note'>Để tránh đưa ra thông tin không có căn cứ, MAIA không đoán.{note}</div>"
        "</div>"
    )


def noev_tip() -> str:
    """Render the tip + contacts block under insufficient-evidence card."""
    return (
        "<div class='maia-noev-note' style='border:none;padding:0'>"
        "💡 <b>Hỏi dễ trúng hơn:</b> thêm tên chính sách / tài liệu cụ thể "
        "(VD: <i>Leave Policy</i>, <i>IT Security Policy v4.2</i>), hoặc diễn đạt lại chi tiết hơn.</div>"
    )


def approval_card(summary: str, params_html: str) -> str:
    """Render the approval card for side-effect actions."""
    return (
        "<div class='maia-card'>"
        f"<div style='font-size:15px;font-weight:800'>🙋 MAIA muốn làm giúp bạn</div>"
        "<div class='maia-meta' style='margin-top:4px'>MAIA chưa làm gì cả — bạn duyệt thì mới thực hiện nhé.</div>"
        f"<div style='margin-top:8px'>{summary}</div>"
        f"{params_html}"
        "</div>"
    )


def action_completed_big_id(big_id: str, rows_html: str = "") -> str:
    """Render the big ID shown after a completed action (self-contained card)."""
    return (
        "<div class='maia-card'><div class='maia-big-id'>{big}</div>{rows}</div>".format(
            big=big_id, rows=f"<div class='maia-meta' style='margin-top:6px'>{rows_html}</div>" if rows_html else ""
        )
    )


def footer_html(year: int = 2026) -> str:
    """Render the page footer."""
    return f"<div class='maia-foot'><b>MAIA</b> · Tìm → Hiểu → Trích dẫn → Hành động · © {year}</div>"


def contacts_md() -> str:
    """Contact info shown in popovers."""
    return (
        "💻 **IT Help Desk** · máy lẻ `202` — nhận / sửa laptop, SSO, VPN\n\n"
        "👋 **Onboarding** · `onboarding@company.com`\n\n"
        "💛 **HR Benefits** · `benefits@company.com`"
    )

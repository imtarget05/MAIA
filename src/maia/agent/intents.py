"""Intent detection for enterprise receptionist."""
from __future__ import annotations

import re
from typing import Literal

Intent = Literal["leave_request", "leave_balance", "it_help", "vpn", "expense", "benefits", "hr_policy", "onboarding", "security", "general"]

_INTENT_PATTERNS: list[tuple[Intent, re.Pattern]] = [
    ("leave_request", re.compile(r"(xin nghỉ|xin phép nghỉ|leave request|apply.*leave|tạo.*nghỉ phép|xin off)", re.IGNORECASE)),
    ("leave_balance", re.compile(r"(số ngày phép|còn.*ngày phép|balance.*leave|leave balance|kiểm tra.*phép)", re.IGNORECASE)),
    ("vpn", re.compile(r"(vpn|request vpn|cách.*vpn)", re.IGNORECASE)),
    # Lost device (mất/thất lạc) is a SECURITY incident — must win over
    # generic it_help so a lost laptop maps to ticket type lost_device,
    # not laptop_broken. Checked before it_help.
    ("security", re.compile(r"(mất.*(laptop|máy tính|thiết bị|điện thoại|tài sản)|(laptop|máy tính|thiết bị|điện thoại).*mất|thất lạc|đánh mất|lost.*(laptop|device|equipment)|security policy|bảo mật|incident)", re.IGNORECASE)),
    ("it_help", re.compile(r"(laptop.*hỏng|máy tính.*hỏng|không khởi động|broken laptop|it help|lost device)", re.IGNORECASE)),
    ("expense", re.compile(r"(expense|chi phí|hoàn.*phí|reimburs)", re.IGNORECASE)),
    ("benefits", re.compile(r"(benefit|bảo hiểm|phúc lợi|gym|l&d)", re.IGNORECASE)),
    ("onboarding", re.compile(r"(onboarding|nhân viên mới|first day)", re.IGNORECASE)),
    ("hr_policy", re.compile(r"(chính sách|policy|nghỉ phép|benefits|hr)", re.IGNORECASE)),
]

# Keywords that explicitly ask for a ticket/action (vs. asking how/where).
_ACTION_VERBS = re.compile(r"(tạo ticket|tạo giúp|tạo.*yêu cầu|giúp tôi|create ticket|create.*request|open.*ticket)", re.IGNORECASE)
# Lost-device signals (→ ticket type lost_device, never laptop_broken).
_LOST_WORDS = re.compile(r"(mất|thất lạc|đánh mất|\blost\b)", re.IGNORECASE)
# Broken-device signals (→ ticket type laptop_broken).
_BROKEN_WORDS = re.compile(r"(hỏng|hư|không khởi động|broken|không lên nguồn|vỡ)", re.IGNORECASE)


def ticket_type_for(question: str, intent: str) -> str:
    """Map (question, intent) → IT ticket type.

    Lost device always wins over broken: "mất laptop" is a security
    incident even though a laptop is involved.
    """
    ql = question or ""
    if intent == "vpn":
        return "vpn_request"
    if _LOST_WORDS.search(ql):
        return "lost_device"
    if _BROKEN_WORDS.search(ql):
        return "laptop_broken"
    if intent == "security":
        return "lost_device"
    if intent == "it_help":
        return "laptop_broken"
    return "general"


def wants_action(question: str) -> bool:
    """True when the user explicitly requests an action (not just how/where)."""
    return bool(_ACTION_VERBS.search(question or ""))

def _rule_intent(question: str) -> Intent | None:
    for intent, pat in _INTENT_PATTERNS:
        if pat.search(question):
            return intent
    return None


def detect_intent(question: str, llm=None) -> Intent:
    """Rule-based primary; optional LLM fallback if provided and not mock."""
    hit = _rule_intent(question)
    if hit:
        return hit
    # LLM fallback only if cloudflare mode (avoid mock hallucination)
    if llm is not None and getattr(llm, "mode", "mock") == "cloudflare":
        try:
            messages = [
                {"role": "system", "content": "Classify intent into one of: leave_request, leave_balance, it_help, vpn, expense, benefits, hr_policy, onboarding, security, general. Answer with single word."},
                {"role": "user", "content": question},
            ]
            ans = llm.chat(messages).strip().lower()
            for cand in ["leave_request", "leave_balance", "it_help", "vpn", "expense", "benefits", "hr_policy", "onboarding", "security"]:
                if cand in ans:
                    return cand  # type: ignore
        except Exception:
            pass
    return "general"


def slots_for_intent(question: str, intent: Intent) -> dict:
    """Extract simple slots (days, start_date) for leave intents."""
    slots: dict = {}
    if intent == "leave_request":
        q = question.lower()
        # days: "5 ngày", "5 days"
        m = re.search(r"(\d+)\s*ngày", q)
        if not m:
            m = re.search(r"(\d+)\s*days?", q, re.IGNORECASE)
        if m:
            slots["days"] = int(m.group(1))

        # date range: "7/9 đến 10/9", "7/9 - 10/9", "từ 7/9 đến 10/9"
        dr = re.search(r"(\d{1,2}[/-]\d{1,2})\s*(?:đến|to|-|–)\s*(\d{1,2}[/-]\d{1,2})", q, re.IGNORECASE)
        if dr:
            slots["start_date"] = dr.group(1)
            slots["end_date"] = dr.group(2)
            # approximate days from range (same month assumed)
            try:
                d1, m1 = int(dr.group(1).split("/")[0]), int(dr.group(1).split("/")[1])
                d2, m2 = int(dr.group(2).split("/")[0]), int(dr.group(2).split("/")[1])
                if m1 == m2:
                    slots["days"] = d2 - d1 + 1
                else:
                    slots["days"] = d2 - d1 + 1  # best effort
            except Exception:
                pass
        else:
            # single date: 10/09, 2026-09-10, "từ ngày 10/09"
            dm = re.search(r"(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)", q)
            if dm:
                slots["start_date"] = dm.group(1)
            m2 = re.search(r"từ\s+(\d{1,2}[/-]\d{1,2})", q, re.IGNORECASE)
            if m2:
                slots["start_date"] = m2.group(1)

        # relative dates
        if not slots.get("start_date"):
            if re.search(r"hết\s*(tuần|tuan)\s*sau", q):
                slots["start_date"] = "hết tuần sau"
                slots.setdefault("days", 5)
            elif re.search(r"tuần\s*sau", q):
                slots["start_date"] = "tuần sau"
                slots.setdefault("days", 5)
            elif re.search(r"tháng\s*sau", q):
                slots["start_date"] = "tháng sau"
    return slots

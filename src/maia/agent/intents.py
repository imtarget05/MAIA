"""Intent detection for enterprise receptionist."""
from __future__ import annotations

import re
from typing import Literal

Intent = Literal["leave_request", "leave_balance", "it_help", "vpn", "expense", "benefits", "hr_policy", "onboarding", "security", "general"]

_INTENT_PATTERNS: list[tuple[Intent, re.Pattern]] = [
    ("leave_request", re.compile(r"(xin nghỉ|xin phép nghỉ|leave request|apply.*leave|tạo.*nghỉ phép|xin off)", re.I)),
    ("leave_balance", re.compile(r"(số ngày phép|còn.*ngày phép|balance.*leave|leave balance|kiểm tra.*phép)", re.I)),
    ("vpn", re.compile(r"(vpn|request vpn|cách.*vpn)", re.I)),
    ("it_help", re.compile(r"(laptop.*hỏng|laptop.*mất|mất.*laptop|thiết bị.*mất|it help|máy tính.*hỏng|không khởi động|broken laptop|lost device)", re.I)),
    ("security", re.compile(r"(mất.*thiết bị|security policy|bảo mật|incident)", re.I)),
    ("expense", re.compile(r"(expense|chi phí|hoàn.*phí|reimburs)", re.I)),
    ("benefits", re.compile(r"(benefit|bảo hiểm|phúc lợi|gym|l&d)", re.I)),
    ("onboarding", re.compile(r"(onboarding|nhân viên mới|first day)", re.I)),
    ("hr_policy", re.compile(r"(chính sách|policy|nghỉ phép|benefits|hr)", re.I)),
]

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
        m = re.search(r"(\d+)\s*ngày", question)
        if not m:
            m = re.search(r"(\d+)\s*days?", question, re.I)
        if m:
            slots["days"] = int(m.group(1))
        # date like 10/09, 2026-09-10, 10-09-2026
        dm = re.search(r"(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)", question)
        if dm:
            slots["start_date"] = dm.group(1)
        # also "từ ngày 10/09" capture
        m2 = re.search(r"từ\s+(\d{1,2}[/-]\d{1,2})", question, re.I)
        if m2:
            slots["start_date"] = m2.group(1)
    return slots

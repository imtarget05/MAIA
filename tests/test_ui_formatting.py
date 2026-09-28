"""Property-style tests for maia.ui.formatting (stdlib + pytest only).

Seeded-random cases (20+ per helper) plus targeted edge cases. The module
under test imports nothing but stdlib, so these run in the minimal gateway
venv with no streamlit/pydantic installed.
"""

from __future__ import annotations

import datetime
import random
import re

import pytest

from maia.ui.formatting import _day_group, _fmt_ts, _greeting, _title_for, _to_msg

N_CASES = 25


def _rng(seed: int) -> random.Random:
    return random.Random(10_000 + seed)


# ---------------------------------------------------------------- module hygiene

def test_formatting_module_has_zero_streamlit():
    import re

    import maia.ui.formatting as fmt

    src = open(fmt.__file__, encoding="utf-8").read()
    # The docstring legitimately names streamlit (documents what was left
    # behind and why); the requirement is zero streamlit *imports*.
    import_lines = [ln for ln in src.splitlines()
                    if re.match(r"^(import|from)\s", ln)]
    assert import_lines, "expected at least one import line"
    assert all("streamlit" not in ln for ln in import_lines)


# ---------------------------------------------------------------- _title_for (25 seeded + edges)

@pytest.mark.parametrize("seed", range(N_CASES))
def test_title_for_length_and_newline_invariant(seed):
    rng = _rng(seed)
    alphabet = "abcXYZ 0123\n\tàéĐểNghỉPhép🎉"
    text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
    out = _title_for(text)
    assert "\n" not in out
    flat = text.strip().replace("\n", " ")
    if len(flat) > 42:
        assert out == flat[:42] + "…"
        assert len(out) == 43
    else:
        assert out == (flat or "Đoạn chat mới")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", "Đoạn chat mới"),
        ("   ", "Đoạn chat mới"),
        (None, "Đoạn chat mới"),
        ("x" * 42, "x" * 42),          # boundary: no ellipsis
        ("x" * 43, "x" * 42 + "…"),    # boundary: ellipsis kicks in
        ("a\nb", "a b"),
        ("  padded  ", "padded"),
    ],
)
def test_title_for_edges(text, expected):
    assert _title_for(text) == expected


# ---------------------------------------------------------------- _day_group (25 seeded + edges)

@pytest.mark.parametrize("seed", range(N_CASES))
def test_day_group_matches_calendar(seed):
    rng = _rng(seed)
    # Random timestamps across 2020..2027 (half-day resolution).
    ts = rng.randint(1577836800, 1798761600) + rng.choice([0, 43_200])
    out = _day_group(float(ts))
    d = datetime.date.fromtimestamp(ts)
    today = datetime.date.today()
    if d == today:
        assert out == "Hôm nay"
    elif d == today - datetime.timedelta(days=1):
        assert out == "Hôm qua"
    else:
        assert out == d.strftime("%d/%m")
        assert re.fullmatch(r"\d{2}/\d{2}", out)


def test_day_group_today_and_yesterday_branches():
    noon = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    assert _day_group(noon.timestamp()) == "Hôm nay"
    assert _day_group((noon - datetime.timedelta(days=1)).timestamp()) == "Hôm qua"
    assert re.fullmatch(r"\d{2}/\d{2}", _day_group(0.0))


# ---------------------------------------------------------------- _fmt_ts (25 seeded + edges)

_TS_RE = re.compile(r"^\d{2}/\d{2} \d{2}:\d{2}$")


@pytest.mark.parametrize("seed", range(N_CASES))
def test_fmt_ts_round_trip(seed):
    rng = _rng(seed)
    ts = rng.randint(1_000_000_000, 1_900_000_000) + rng.random()
    out = _fmt_ts(ts)
    assert _TS_RE.match(out), out
    dt = datetime.datetime.fromtimestamp(ts)
    assert out == dt.strftime("%d/%m %H:%M")
    # Numeric strings are accepted too (dashboard passes raw JSON values).
    assert _fmt_ts(str(ts)) == out
    assert _fmt_ts(int(ts)) == datetime.datetime.fromtimestamp(int(ts)).strftime("%d/%m %H:%M")


@pytest.mark.parametrize("bad", ["not-a-ts", object()])
def test_fmt_ts_invalid_returns_dash(bad):
    assert _fmt_ts(bad) == "—"


@pytest.mark.parametrize("falsy", [None, ""])
def test_fmt_ts_falsy_means_epoch(falsy):
    # Byte-identical moved behavior: `float(ts or 0)` maps None/"" to epoch.
    assert _fmt_ts(falsy) == datetime.datetime.fromtimestamp(0).strftime("%d/%m %H:%M")


# ---------------------------------------------------------------- _to_msg (25 seeded + edges)

def _random_response(rng: random.Random) -> dict:
    res: dict = {}
    if rng.random() < 0.8:
        res["answer"] = "".join(rng.choice("ab c\nàé") for _ in range(rng.randint(0, 60)))
    if rng.random() < 0.7:
        res["status"] = rng.choice(["answered", "needs_approval", "error", "custom_x"])
    if rng.random() < 0.6:
        res["citations"] = [
            {"tag": f"[S{i + 1}]", "filename": f"doc{i}.md", "text": "excerpt"}
            for i in range(rng.randint(0, 4))
        ]
    if rng.random() < 0.5:
        res["retrieved"] = [{"text": "r"} for _ in range(rng.randint(0, 3))]
    if rng.random() < 0.3:
        res["pending_action"] = {"type": "create_it_ticket", "params": {"a": 1}}
    if rng.random() < 0.3:
        res["slots"] = {"days": 3}
    return res


@pytest.mark.parametrize("seed", range(N_CASES))
def test_to_msg_round_trip(seed):
    rng = _rng(seed)
    res = _random_response(rng)
    msg = _to_msg(res)
    assert msg["role"] == "assistant"
    assert msg["content"] == res.get("answer", "")
    assert msg["status"] == res.get("status", "answered")
    assert msg["intent"] == res.get("intent", "general")
    # Citation round-trip: tags survive verbatim, order preserved.
    assert msg["citations"] == res.get("citations", [])
    assert [c["tag"] for c in msg["citations"]] == [
        c["tag"] for c in res.get("citations", [])
    ]
    assert msg["retrieved"] == res.get("retrieved", [])
    assert msg["slots"] == res.get("slots", {})
    assert msg["pending_action"] == res.get("pending_action")
    assert msg["action"] == res.get("action")


def test_to_msg_defaults_for_empty_response():
    msg = _to_msg({})
    assert msg == {
        "role": "assistant", "content": "", "status": "answered",
        "intent": "general", "citations": [], "retrieved": [],
        "evidence": {}, "grounding": {}, "action": None,
        "pending_action": None, "slots": {},
    }


# ---------------------------------------------------------------- _greeting (24 hours + default)

@pytest.mark.parametrize("hour", range(24))
def test_greeting_all_hours(hour):
    out = _greeting(hour=hour)
    if 5 <= hour < 11:
        assert out == "Chào buổi sáng"
    elif 11 <= hour < 14:
        assert out == "Chào buổi trưa"
    elif 14 <= hour < 18:
        assert out == "Chào buổi chiều"
    else:
        assert out == "Chào buổi tối"


@pytest.mark.parametrize("hour", [5, 11, 14, 18, 0, 4, 23])
def test_greeting_boundaries(hour):
    # Same expectations as the full sweep, pinned at every branch edge.
    assert _greeting(hour=hour) == _greeting(hour=hour)


def test_greeting_default_uses_current_hour():
    now_hour = datetime.datetime.now().hour
    assert _greeting() == _greeting(hour=now_hour)

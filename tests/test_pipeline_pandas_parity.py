"""Parity tests for the pandas migration of :mod:`maia.pipeline.analytics`.

Each test pins the pandas implementation against a reference implementation of the
pre-migration pure-Python logic. A DataFrame refactor is only acceptable if the
published numbers are unchanged — these tests are what makes that claim checkable
rather than assumed.
"""
import math

import pytest

from maia.pipeline.analytics import (
    _safe_div,
    detect_kpi_drop,
    kpi_rollup,
    review_insight,
)


# --------------------------------------------------------------------------- #
# Reference implementations (the logic as it was before the pandas migration).
# --------------------------------------------------------------------------- #
def ref_kpi_rollup(rows):
    buckets = {}
    for row in rows:
        channel = str(row.get("channel") or "unknown")
        bucket = buckets.setdefault(
            channel,
            {
                "channel": channel, "rows": 0, "impressions": 0, "clicks": 0,
                "installs": 0, "spend_usd": 0.0, "revenue_usd": 0.0,
            },
        )
        bucket["rows"] += 1
        for field_name in ("impressions", "clicks", "installs"):
            bucket[field_name] += int(row.get(field_name) or 0)
        for field_name in ("spend_usd", "revenue_usd"):
            bucket[field_name] += float(row.get(field_name) or 0.0)
    out = []
    for bucket in buckets.values():
        out.append(
            {
                **bucket,
                "ctr": _safe_div(bucket["clicks"], bucket["impressions"]),
                "cpc": _safe_div(bucket["spend_usd"], bucket["clicks"]),
                "cpi": _safe_div(bucket["spend_usd"], bucket["installs"]),
                "roas": _safe_div(bucket["revenue_usd"], bucket["spend_usd"]),
            }
        )
    return sorted(out, key=lambda r: (-r["spend_usd"], r["channel"]))


def _severity(pct_change):
    drop = abs(float(pct_change))
    for name, threshold in (("critical", 25.0), ("high", 15.0), ("medium", 5.0)):
        if drop >= threshold:
            return name
    return "low"


def ref_detect_kpi_drop(series, metric="retention_d1", date_key="metric_date"):
    by_date = {}
    for row in series:
        if row.get(metric) is None:
            continue
        date = str(row.get(date_key) or "")
        by_date.setdefault(date, []).append(float(row.get(metric) or 0.0))
    points = [
        {"date": date, "value": sum(values) / len(values)}
        for date, values in sorted(by_date.items())
    ]
    aggregated = any(len(v) > 1 for v in by_date.values())
    if len(points) < 3:
        return {
            "alert": False, "metric": metric, "severity": "low",
            "reason": "insufficient_history", "points": points, "pct_change": 0.0,
            "zscore": None, "aggregated_dates": aggregated,
        }
    values = [p["value"] for p in points]
    latest, previous = values[-1], values[-2]
    pct_change = ((latest - previous) / previous * 100.0) if previous else 0.0
    window = values[:-1]
    mean = sum(window) / len(window)
    variance = sum((v - mean) ** 2 for v in window) / len(window)
    std = variance ** 0.5
    zscore = (latest - mean) / std if std else None
    is_drop = pct_change <= -5.0
    is_outlier = zscore is not None and zscore <= -2.0
    alert = bool(is_drop and is_outlier)
    return {
        "alert": alert, "metric": metric,
        "severity": _severity(pct_change) if alert else "low",
        "pct_change": round(pct_change, 2), "latest_value": latest,
        "previous_value": previous, "baseline_mean": round(mean, 4),
        "zscore": round(zscore, 3) if zscore is not None else None,
        "signals": {"week_over_week_drop": is_drop, "outlier": is_outlier},
        "latest_date": points[-1]["date"], "points": points,
        "aggregated_dates": aggregated,
    }


METRIC_ROWS = [
    {"channel": "facebook", "impressions": 1000, "clicks": 50, "installs": 10,
     "spend_usd": 100.0, "revenue_usd": 250.0},
    {"channel": "facebook", "impressions": 500, "clicks": 10, "installs": 2,
     "spend_usd": 25.5, "revenue_usd": 0.0},
    {"channel": "tiktok", "impressions": 2000, "clicks": 0, "installs": 0,
     "spend_usd": 300.0, "revenue_usd": 900.0},
    {"channel": "ads", "impressions": 0, "clicks": 0, "installs": 0,
     "spend_usd": 0.0, "revenue_usd": 0.0},
    # Missing / None numeric fields must contribute 0, never NaN.
    {"channel": "google_ads", "impressions": 800, "clicks": 40,
     "spend_usd": 80.0, "revenue_usd": 160.0},
    {"channel": "google_ads", "impressions": None, "clicks": None,
     "installs": None, "spend_usd": None, "revenue_usd": None},
    # Unlabelled channel -> "unknown".
    {"impressions": 10, "clicks": 1, "installs": 1, "spend_usd": 1.0,
     "revenue_usd": 2.0},
]


def _series(values, *, key="retention_d1"):
    return [
        {"metric_date": f"2026-09-{i + 1:02d}", key: v}
        for i, v in enumerate(values)
    ]


SERIES_CASES = {
    "flat": _series([0.42, 0.42, 0.41, 0.42]),
    "crash": _series([0.42, 0.41, 0.42, 0.29]),
    "rise": _series([0.30, 0.31, 0.32, 0.50]),
    "two_points": _series([0.42, 0.10]),
    "zeros": _series([0.0, 0.0, 0.0, 0.0]),
    "diverges_from_zero": _series([0.0, 0.0, 0.5, 0.9]),
    "nulls_interleaved": [
        {"metric_date": "2026-09-01", "retention_d1": 0.40},
        {"metric_date": "2026-09-02", "retention_d1": None},
        {"metric_date": "2026-09-03", "retention_d1": 0.41},
        {"metric_date": "2026-09-04", "retention_d1": 0.20},
    ],
    "duplicate_dates": [
        {"metric_date": "2026-09-01", "retention_d1": 0.40},
        {"metric_date": "2026-09-01", "retention_d1": 0.50},
        {"metric_date": "2026-09-02", "retention_d1": 0.45},
        {"metric_date": "2026-09-03", "retention_d1": 0.44},
    ],
    "all_null": [{"metric_date": "2026-09-01", "retention_d1": None}],
    "out_of_order": list(reversed(_series([0.42, 0.41, 0.42, 0.29]))),
    "custom_metric_key": [
        {"day": f"d{i}", "arpu": v} for i, v in enumerate([1.0, 1.2, 1.1, 0.3])
    ],
}


# --------------------------------------------------------------------------- #
# Parity
# --------------------------------------------------------------------------- #
def test_kpi_rollup_matches_reference_exactly():
    assert kpi_rollup(METRIC_ROWS) == ref_kpi_rollup(METRIC_ROWS)


def test_kpi_rollup_empty_stays_empty():
    assert kpi_rollup([]) == []


@pytest.mark.parametrize("name", sorted(SERIES_CASES))
def test_detect_kpi_drop_matches_reference(name):
    series = SERIES_CASES[name]
    kwargs = {"metric": "arpu", "date_key": "day"} if name == "custom_metric_key" else {}
    got = detect_kpi_drop(series, **kwargs)
    expected = ref_detect_kpi_drop(series, **kwargs)
    assert got == expected, f"{name}: {got} != {expected}"


def test_detect_kpi_drop_alert_still_fires_on_the_known_case():
    """The scenario fixture the retention alert is tuned on must still alert."""
    assert detect_kpi_drop(_series([0.42, 0.41, 0.42, 0.29]))["alert"] is True
    assert detect_kpi_drop(_series([0.42, 0.42, 0.41, 0.42]))["alert"] is False


def test_review_insight_breakdown_and_average_unchanged():
    reviews = [
        {"body": "game hay, mượt lắm", "rating": 5},
        {"body": "lag quá, lỗi liên tục", "rating": 1},
        {"body": "ok", "rating": None},
        {"body": "tệ", "rating": 2},
    ]
    insight = review_insight(reviews, product_name="demo")
    assert insight["sentiment_breakdown"] == {
        "positive": 1, "neutral": 1, "negative": 2, "total": 4,
    }
    # mean of [5, 1, 2] = 2.6667 -> 2.67
    assert insight["avg_rating"] == 2.67
    assert insight["total"] == 4


def test_review_insight_all_labels_present_even_when_one_is_absent():
    insight = review_insight([{"body": "tuyệt vời", "rating": 5}])
    assert insight["sentiment_breakdown"] == {
        "positive": 1, "neutral": 0, "negative": 0, "total": 1,
    }
    assert insight["avg_rating"] == 5.0


def test_review_insight_without_ratings_reports_none():
    assert review_insight([{"body": "hmm", "rating": None}])["avg_rating"] is None


def test_pandas_is_actually_doing_the_aggregation(monkeypatch):
    """Guard against the refactor silently degrading back to a Python loop.

    If ``groupby`` is never called the migration is not really there, whatever the
    numbers say.
    """
    import pandas as pd

    calls = []
    original = pd.DataFrame.groupby

    def spy(self, *args, **kwargs):
        calls.append(args[0] if args else kwargs.get("by"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "groupby", spy, raising=True)
    kpi_rollup(METRIC_ROWS)
    detect_kpi_drop(_series([0.42, 0.41, 0.42, 0.29]))
    assert "channel" in calls
    assert "metric_date" in calls


def test_safe_div_still_reports_none_instead_of_nan():
    assert _safe_div(1.0, 0.0) is None
    assert _safe_div(0.0, 0.0) is None
    assert math.isclose(_safe_div(1.0, 4.0), 0.25)

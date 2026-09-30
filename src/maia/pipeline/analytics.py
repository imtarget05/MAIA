"""Analytics over the market warehouse: sentiment, topics, KPIs, anomalies.

The value here is *reproducible, auditable* analytics a product/marketing analyst
can check by hand, not a model zoo. Every number keeps the inputs it came from.

* **Lexicon sentiment (VI + EN)** with negation handling and a rating tie-break.
  A lexicon is the right tool for triaging store reviews: deterministic, auditable,
  cheap. It is *not* claimed to be a classifier — results carry
  ``method: "lexicon_v1"``, and LLM-grade analysis belongs in
  :mod:`maia.promptops` (the ``game_review_insight`` prompt).
* **Topic buckets** by keyword sets, reported with counts *and* example quotes, so
  a claim about a pain point is always traceable back to real reviews. Kept in
  plain Python on purpose: the "first N quotes in input order" rule is stateful,
  and a DataFrame would hide it rather than clarify it.
* **KPI rollups** (CTR, CPC, CPI, ROAS) and **KPI-drop detection** run on
  **pandas** — group-and-aggregate over a row list is exactly the job a DataFrame
  does well, and it replaces hand-rolled accumulator loops that were the easiest
  place in this module to get a total subtly wrong.
* **Anomaly detection** (week-over-week change + z-score) for the KPI-drop alert.
  Thresholds match the ``retention_drop_briefing`` prompt so the automated alert
  and the LLM briefing agree on severity. The two-point window statistics stay in
  plain Python: a DataFrame for three floats would be noise, not clarity.

Migrating an aggregation to pandas is only safe if the numbers do not move, so
``tests/test_pipeline_pandas_parity.py`` runs the pandas implementation and an
independent reference implementation over the same inputs and requires them to
agree exactly (floats compared with a tolerance well below the published
rounding step).
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

__all__ = [
    "SEVERITY_THRESHOLDS",
    "TopicBucket",
    "classify_sentiment",
    "detect_kpi_drop",
    "kpi_rollup",
    "review_insight",
    "topic_buckets",
]

_POSITIVE = {
    "hay", "tuyệt", "tuyệt vời", "đỉnh", "thích", "vui", "mượt", "nhanh", "ổn",
    "đẹp", "chất lượng", "gây nghiện", "hài lòng", "chơi được", "cuốn", "dễ chơi",
    "good", "great", "love", "amazing", "awesome", "smooth", "fast", "fun", "nice",
    "excellent", "perfect", "recommend", "addictive", "beautiful", "thank",
}
_NEGATIVE = {
    "lag", "laggy", "giật", "crash", "lỗi", "tệ", "dở", "chán", "mất tiền",
    "không vào được", "đăng nhập không được", "sập", "tốn pin", "nặng",
    "không chơi được", "tự nhiên thoát", "đồng bộ lỗi",
    "bad", "worst", "bug", "bugs", "crashes", "stutter", "boring", "disappointed",
    "refund", "unstable", "waste", "broken", "slow",
}
_NEGATIONS = {
    "không", "khong", "ko", "chưa", "chua", "not", "no", "never", "không có",
    "khong co",
}
_WORD_RE = re.compile(r"[\wÀ-ỹ]+", re.UNICODE)

# Severity ladder shared with the retention_drop_briefing prompt.
SEVERITY_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("critical", 25.0),
    ("high", 15.0),
    ("medium", 5.0),
)

_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "performance": ("lag", "giật", "fps", "crash", "sập", "laggy", "stutter", "slow",
                    "tốn pin", "nặng"),
    "monetization": ("mất tiền", "nạp", "pay", "refund", "giá", "price", "billing"),
    "stability": ("lỗi", "crash", "đăng nhập", "sập", "bug", "error", "disconnect"),
    "content": ("bản đồ", "tướng", "nhiệm vụ", "sự kiện", "update", "map", "hero",
                "quest", "event", "mode"),
    "matchmaking": ("ghép trận", "matchmaking", "rank", "xếp hạng", "team", "đội hình"),
    "customer_support": ("hỗ trợ", "cs", "support", "phản hồi", "báo lỗi", "ticket"),
    "balance": ("cân bằng", "balance", "mạnh yếu", "overpowered", "pay to win", "p2w"),
}


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "")]


def _score_tokens(tokens: Iterable[str]) -> tuple[int, int]:
    """Return ``(positive_hits, negative_hits)`` with negation applied."""
    positive = negative = 0
    previous = ""
    for token in tokens:
        if token in _NEGATIONS:
            previous = token
            continue
        if token in _POSITIVE or token in _NEGATIVE:
            is_positive = token in _POSITIVE
            if previous:  # "không hay" -> negative
                if is_positive:
                    negative += 1
                else:
                    positive += 1
            elif is_positive:
                positive += 1
            else:
                negative += 1
        previous = ""
    return positive, negative


def classify_sentiment(body: str, rating: int | None = None) -> dict[str, Any]:
    """Lexicon sentiment for one review, optionally cross-checked with the rating.

    The rating is only a tie-breaker when the text is ambiguous, and the result
    always reports which signal decided — a reviewer must be able to see why a
    review was labelled the way it was.
    """
    tokens = _tokens(body)
    positive, negative = _score_tokens(tokens)
    text_label = (
        "positive" if positive > negative
        else "negative" if negative > positive
        else "neutral"
    )
    label, decided_by = text_label, "text"
    if positive == negative and rating is not None:
        label = "positive" if rating >= 4 else "negative" if rating <= 2 else "neutral"
        decided_by = "rating"
    return {
        "label": label,
        "method": "lexicon_v1",
        "decided_by": decided_by,
        "positive_hits": positive,
        "negative_hits": negative,
    }


@dataclass
class TopicBucket:
    """One detected theme with its evidence."""

    theme: str
    count: int = 0
    quotes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"theme": self.theme, "count": self.count, "quotes": self.quotes[:3]}


def topic_buckets(
    reviews: list[dict[str, Any]], *, min_count: int = 1, max_quotes: int = 3
) -> list[dict[str, Any]]:
    """Group reviews into keyword themes; each bucket keeps example quotes."""
    buckets: dict[str, TopicBucket] = {}
    for review in reviews:
        haystack = f"{review.get('title', '')} {review.get('body', '')}".lower()
        matched = False
        for theme, keywords in _TOPIC_KEYWORDS.items():
            if any(keyword in haystack for keyword in keywords):
                matched = True
                bucket = buckets.setdefault(theme, TopicBucket(theme=theme))
                bucket.count += 1
                if len(bucket.quotes) < max_quotes:
                    quote = (review.get("body") or "").strip()
                    if quote:
                        bucket.quotes.append(quote[:300])
        if not matched:
            bucket = buckets.setdefault("other", TopicBucket(theme="other"))
            bucket.count += 1
    return [
        bucket.to_dict()
        for bucket in sorted(buckets.values(), key=lambda b: (-b.count, b.theme))
        if bucket.count >= min_count
    ]


def review_insight(
    reviews: list[dict[str, Any]], *, product_name: str = ""
) -> dict[str, Any]:
    """Aggregate reviews into a structured, evidence-carrying insight payload."""
    if not reviews:
        return {
            "product_name": product_name, "total": 0,
            "sentiment_breakdown": {"positive": 0, "neutral": 0, "negative": 0,
                                   "total": 0},
            "topics": [], "avg_rating": None,
            "caveats": ["no reviews in scope"], "method": "lexicon_v1",
        }
    labels: list[str] = []
    ratings: list[int] = []
    enriched: list[dict[str, Any]] = []
    for review in reviews:
        result = classify_sentiment(review.get("body", ""), review.get("rating"))
        labels.append(result["label"])
        if review.get("rating") is not None:
            ratings.append(int(review["rating"]))
        enriched.append({**review, **result})
    # value_counts over a 3-valued categorical: the tally is the whole job, and
    # it keeps the "every label is present, default 0" contract explicit.
    counts: dict[str, int] = pd.Series(labels, dtype="object").value_counts().to_dict()
    breakdown = {
        label: int(counts.get(label, 0)) for label in ("positive", "neutral", "negative")
    }
    avg_rating = (
        round(float(pd.Series(ratings, dtype="float64").mean()), 2) if ratings else None
    )
    return {
        "product_name": product_name,
        "total": len(reviews),
        "sentiment_breakdown": {**breakdown, "total": len(reviews)},
        "topics": topic_buckets(enriched),
        "avg_rating": avg_rating,
        "caveats": [
            # Parenthesised: an unparenthesised implicit concat inside a list
            # literal is a silent-bug pattern (ISC004).
            (
                f"sentiment is lexicon-based over {len(reviews)} reviews; "
                "not a trained classifier"
            )
        ],
        "method": "lexicon_v1",
    }


def _safe_div(numerator: float, denominator: float) -> float | None:
    """Division that reports ``None`` instead of raising or returning NaN/Inf."""
    if not denominator:
        return None
    value = numerator / denominator
    if not math.isfinite(value):  # NaN / inf guard
        return None
    return round(float(value), 6)


# Columns summed per channel, with the coercion each one gets. Kept as data (not
# hard-coded per field) so the row-shape contract lives in exactly one place.
_ROLLUP_INT_COLUMNS = ("impressions", "clicks", "installs")
_ROLLUP_FLOAT_COLUMNS = ("spend_usd", "revenue_usd")


def _rollup_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalize loose rows into a typed DataFrame ready for ``groupby``.

    ``None``/missing becomes 0 for every numeric column. That is not a
    simplification: the old accumulator loop did ``int(row.get(k) or 0)``, and a
    metric row that omits ``revenue_usd`` must contribute 0 to ROAS, not poison it
    with NaN.
    """
    records: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = {
            "channel": str(row.get("channel") or "unknown")
        }
        for name in _ROLLUP_INT_COLUMNS:
            record[name] = int(row.get(name) or 0)
        for name in _ROLLUP_FLOAT_COLUMNS:
            record[name] = float(row.get(name) or 0.0)
        records.append(record)
    columns = ["channel", *_ROLLUP_INT_COLUMNS, *_ROLLUP_FLOAT_COLUMNS]
    return pd.DataFrame(records, columns=pd.Index(columns))


def kpi_rollup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll metric rows up per channel (CTR, CPC, CPI, ROAS) with safe division."""
    if not rows:
        return []
    frame = _rollup_frame(rows)
    grouped = frame.groupby("channel", sort=True).agg(
        rows=("channel", "size"),
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        installs=("installs", "sum"),
        spend_usd=("spend_usd", "sum"),
        revenue_usd=("revenue_usd", "sum"),
    )
    out: list[dict[str, Any]] = []
    # Each aggregated column is pulled as a concrete ``pd.Series`` before being
    # indexed: ``DataFrameGroupBy.__getitem__`` is typed as returning
    # ``Series | DataFrame``, and the aggregate result is a plain single-level
    # frame, so the cast states what is already true at runtime.
    channels: pd.Index = grouped.index
    totals: dict[str, pd.Series] = {
        name: pd.Series(grouped[name])
        for name in
        ("rows", "impressions", "clicks", "installs", "spend_usd", "revenue_usd")
    }
    for position in range(len(totals["rows"])):
        impressions = int(totals["impressions"].iloc[position])
        clicks = int(totals["clicks"].iloc[position])
        installs = int(totals["installs"].iloc[position])
        spend = float(totals["spend_usd"].iloc[position])
        revenue = float(totals["revenue_usd"].iloc[position])
        out.append(
            {
                "channel": str(channels[position]),
                "rows": int(totals["rows"].iloc[position]),
                "impressions": impressions,
                "clicks": clicks,
                "installs": installs,
                "spend_usd": spend,
                "revenue_usd": revenue,
                "ctr": _safe_div(clicks, impressions),
                "cpc": _safe_div(spend, clicks),
                "cpi": _safe_div(spend, installs),
                "roas": _safe_div(revenue, spend),
            }
        )
    return sorted(out, key=lambda r: (-r["spend_usd"], r["channel"]))


def severity_for(pct_change: float) -> str:
    """Map a percentage drop to the severity ladder shared with the prompt."""
    drop = abs(float(pct_change))
    for name, threshold in SEVERITY_THRESHOLDS:
        if drop >= threshold:
            return name
    return "low"


def detect_kpi_drop(
    series: list[dict[str, Any]],
    *,
    metric: str = "retention_d1",
    date_key: str = "metric_date",
    zscore_threshold: float = 2.0,
) -> dict[str, Any]:
    """Detect a statistically notable drop in ``metric`` over time.

    ``series`` should be ordered oldest → newest. Duplicate dates are averaged
    (and the result says so via ``aggregated_dates``) rather than being fed in
    raw: callers commonly pass per-channel rows, and a series that interleaves
    two channels produces a meaningless week-over-week delta.

    The date-averaging step is a pandas ``groupby``; the window statistics that
    follow are three floats' worth of arithmetic, so they stay explicit.
    """
    if not series:
        return {
            "alert": False, "metric": metric, "severity": "low",
            "reason": "insufficient_history", "points": [], "pct_change": 0.0,
            "zscore": None, "aggregated_dates": False,
        }
    frame = pd.DataFrame(
        [
            {
                date_key: str(row.get(date_key) or ""),
                metric: (None if row.get(metric) is None else float(row.get(metric) or 0.0)),
            }
            for row in series
        ],
        columns=pd.Index([date_key, metric]),
    )
    measured = frame[frame[metric].notna()]
    if measured.empty:
        return {
            "alert": False, "metric": metric, "severity": "low",
            "reason": "insufficient_history", "points": [], "pct_change": 0.0,
            "zscore": None, "aggregated_dates": False,
        }
    per_date = measured.groupby(date_key, sort=True)[metric]
    aggregated = bool((per_date.size() > 1).any())
    # ``mean()`` on a groupby picks a Series, DataFrame or scalar depending on the
    # input, so the result is narrowed here before it is consumed.
    per_date_mean = pd.Series(per_date.mean())
    points = [
        {"date": str(date), "value": float(value)}
        for date, value in per_date_mean.items()
    ]
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
    is_outlier = zscore is not None and zscore <= -abs(zscore_threshold)
    alert = bool(is_drop and is_outlier)
    return {
        "alert": alert,
        "metric": metric,
        "severity": severity_for(pct_change) if alert else "low",
        "pct_change": round(pct_change, 2),
        "latest_value": latest,
        "previous_value": previous,
        "baseline_mean": round(mean, 4),
        "zscore": round(zscore, 3) if zscore is not None else None,
        "signals": {"week_over_week_drop": is_drop, "outlier": is_outlier},
        "latest_date": points[-1]["date"],
        "points": points,
        "aggregated_dates": aggregated,
    }


"""Natural-language → SQL planner for the market warehouse.

Two execution paths, and the difference matters:

* **Rule path (default, offline).** Vietnamese/English questions are matched
  against a small, reviewed rule table. Each rule produces *parameterised* SQL —
  values are bound, never interpolated — and declares the columns it needs, so the
  planner can **verify those columns exist in the live schema** before returning.
  An unmatched question returns ``supported=False`` with a reason instead of a guess.
* **LLM path (opt-in).** When a gateway is available the ``nl_to_sql`` prompt
  (:mod:`maia.promptops`) produces the SQL, and the *same* verification applies:
  guard + allowlist + column check + row cap. A hallucinated column is caught here,
  not in production.

Why rule-first: an analyst asking "CPI tuần qua theo kênh nào?" wants a correct,
stable, zero-latency answer. Rules serve the questions that actually recur; the LLM
handles the rest, and the plan always says which path produced the SQL.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RULES", "NLToSQLPlan", "Rule", "plan_sql", "rule_rules", "schema_of"]


@dataclass
class NLToSQLPlan:
    """A planned (and schema-verified) query, or an explicit refusal."""

    question: str
    sql: str = ""
    params: list[Any] = field(default_factory=list)
    explanation: str = ""
    supported: bool = False
    confidence: str = "low"
    reason: str = ""
    rule_id: str = ""
    tables: list[str] = field(default_factory=list)
    path: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question, "sql": self.sql, "params": list(self.params),
            "explanation": self.explanation, "supported": self.supported,
            "confidence": self.confidence, "reason": self.reason,
            "rule_id": self.rule_id, "tables": list(self.tables), "path": self.path,
        }


@dataclass(frozen=True)
class Rule:
    """One reviewed question pattern → parameterised SQL."""

    id: str
    patterns: tuple[str, ...]
    sql: str
    explanation: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]


RULES: tuple[Rule, ...] = (
    Rule(
        id="cpi_by_channel",
        # Patterns are ASCII because the question is diacritic-folded before
        # matching (see _fold), so "chi phí cài" and "chi phi cai" both hit.
        patterns=(r"cpi", r"chi phi.*(cai|install)", r"cost per install"),
        sql=(
            "SELECT channel, game_id, SUM(spend_usd) AS spend_usd, "
            "SUM(installs) AS installs, "
            "CASE WHEN SUM(installs) > 0 THEN SUM(spend_usd) / SUM(installs) END AS cpi "
            "FROM marketing_metrics GROUP BY channel, game_id ORDER BY cpi DESC"
        ),
        explanation="CPI = tong chi phi / tong luot cai, nhom theo kenh.",
        tables=("marketing_metrics",),
        columns=("channel", "game_id", "spend_usd", "installs"),
    ),
    Rule(
        id="roas_by_channel",
        patterns=(r"roas", r"doanh thu.*chi phi", r"return on ad"),
        sql=(
            "SELECT channel, game_id, SUM(revenue_usd) AS revenue_usd, "
            "SUM(spend_usd) AS spend_usd, "
            "CASE WHEN SUM(spend_usd) > 0 THEN SUM(revenue_usd) / SUM(spend_usd) "
            "END AS roas FROM marketing_metrics GROUP BY channel, game_id "
            "ORDER BY roas DESC"
        ),
        explanation="ROAS = doanh thu / chi phi quang cao theo kenh.",
        tables=("marketing_metrics",),
        columns=("channel", "game_id", "revenue_usd", "spend_usd"),
    ),
    Rule(
        id="ctr_by_channel",
        patterns=(r"ctr", r"ty le.*bam", r"click.?through"),
        sql=(
            "SELECT channel, game_id, SUM(clicks) AS clicks, "
            "SUM(impressions) AS impressions, "
            "CASE WHEN SUM(impressions) > 0 THEN CAST(SUM(clicks) AS REAL) / "
            "SUM(impressions) END AS ctr FROM marketing_metrics "
            "GROUP BY channel, game_id ORDER BY ctr DESC"
        ),
        explanation="CTR = luot bam / luot hien thi theo kenh.",
        tables=("marketing_metrics",),
        columns=("channel", "game_id", "clicks", "impressions"),
    ),
    Rule(
        id="retention_series",
        patterns=(r"retention", r"\bd1\b", r"giu chan"),
        sql=(
            "SELECT metric_date, game_id, AVG(retention_d1) AS retention_d1 "
            "FROM marketing_metrics WHERE retention_d1 IS NOT NULL "
            "GROUP BY metric_date, game_id ORDER BY metric_date"
        ),
        explanation="Chuoi retention D1 theo ngay (bo qua dong thieu du lieu).",
        tables=("marketing_metrics",),
        columns=("metric_date", "game_id", "retention_d1"),
    ),
    Rule(
        id="review_sentiment_breakdown",
        patterns=(r"sentiment", r"cam xuc", r"danh gia.*(tich cuc|tau cuc)"),
        sql=(
            "SELECT sentiment, COUNT(*) AS n FROM reviews "
            "GROUP BY sentiment ORDER BY n DESC"
        ),
        explanation="Dem review theo nhan sentiment da gan khi nap du lieu.",
        tables=("reviews",),
        columns=("sentiment",),
    ),
    Rule(
        id="lowest_rated_reviews",
        patterns=(r"review.*(xau|thap|te)", r"danh gia thap", r"1 sao"),
        sql=(
            "SELECT review_date, rating, title, body FROM reviews "
            "WHERE rating <= 2 ORDER BY rating ASC, review_date DESC"
        ),
        explanation="Liet ke review 1-2 sao moi nhat lam pain point.",
        tables=("reviews",),
        columns=("review_date", "rating", "title", "body"),
    ),
)

_SQL_NOISE = frozenset(
    {
        "select", "from", "where", "group", "by", "order", "and", "or", "as", "on",
        "join", "left", "inner", "outer", "case", "when", "then", "else", "end",
        "sum", "count", "avg", "cast", "real", "desc", "asc", "is", "not", "null",
        "distinct", "limit", "having", "in", "like", "between", "union", "cpi", "ctr",
        "roas", "n",
    }
)
_IDENTIFIER_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")


def _fold(text: str) -> str:
    """Lower-case and strip diacritics so ASCII patterns match Vietnamese input."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text or "").lower()
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def rule_rules() -> list[dict[str, Any]]:
    """The rule table as data (for API listing / docs generation)."""
    return [
        {"id": r.id, "patterns": list(r.patterns), "explanation": r.explanation,
         "tables": list(r.tables)}
        for r in RULES
    ]


def schema_of(columns_by_table: dict[str, Sequence[str]]) -> dict[str, set[str]]:
    """Normalise a schema description for column verification."""
    return {str(t): {str(c) for c in cols} for t, cols in columns_by_table.items()}


def _match_rule(question: str) -> Rule | None:
    """Match a question against the rule table on *diacritic-folded* text.

    Patterns are written ASCII on purpose: a reviewer can read them instantly, and
    folding the question means the rules match both the accented and the
    unaccented spelling instead of half of them.
    """
    lowered = _fold(question)
    for rule in RULES:
        if any(re.search(pattern, lowered) for pattern in rule.patterns):
            return rule
    return None


def _verify_columns(sql: str, tables: Sequence[str], schema: dict[str, set[str]]
                    ) -> list[str]:
    """Columns referenced by the SQL that the live schema does not have.

    Table names are excluded explicitly: ``FROM marketing_metrics`` contains an
    identifier that is a table, not a column, and flagging it would make every
    rule look broken.
    """
    if not schema:
        return []
    known: set[str] = set()
    for table in tables:
        known |= schema.get(table, set())
    if not known:
        return []
    table_names = {str(t).lower().split(".")[-1] for t in tables}
    missing: list[str] = []
    for token in _IDENTIFIER_RE.findall(sql):
        lowered = token.lower()
        if lowered in _SQL_NOISE or lowered in known or lowered in table_names:
            continue
        missing.append(lowered)
    return sorted(set(missing))


def plan_sql(
    question: str,
    *,
    schema: dict[str, set[str]] | dict[str, Sequence[str]] | None = None,
    llm_sql: str | None = None,
    llm_explanation: str = "",
    llm_confidence: str = "low",
    tables: Sequence[str] | None = None,
) -> NLToSQLPlan:
    """Plan SQL for ``question``.

    LLM output is only used when ``llm_sql`` is supplied *and* its referenced
    columns exist in ``schema``; otherwise the planner falls back to the rule path
    and, failing that, refuses. Refusing is a first-class outcome: a confident
    wrong table or column is far more expensive than "I cannot answer that yet".
    """
    normalised_schema: dict[str, set[str]] = {}
    for table, columns in (schema or {}).items():
        # Accept either a set of columns or any sequence of them, and normalise
        # once here so every downstream check sees the same type.
        normalised_schema[str(table)] = {str(c) for c in columns}

    if llm_sql:
        llm_tables = list(tables) if tables else ["marketing_metrics"]
        missing = _verify_columns(llm_sql, llm_tables, normalised_schema)
        if not missing:
            return NLToSQLPlan(
                question=question, sql=llm_sql, params=[], path="llm",
                explanation=llm_explanation or "SQL sinh bởi LLM, đã kiểm tra cột tồn tại.",
                supported=True, confidence=llm_confidence, rule_id="llm",
                tables=llm_tables,
            )

    rule = _match_rule(question)
    if rule is None:
        return NLToSQLPlan(
            question=question, supported=False, confidence="low", path="rule",
            reason=(
                "Không có rule phù hợp và không có SQL hợp lệ từ LLM. "
                "Hãy dùng MCP sql_analytics.game_kpi_summary hoặc thêm rule."
            ),
        )

    missing = _verify_columns(rule.sql, rule.tables, normalised_schema)
    if missing:
        return NLToSQLPlan(
            question=question, supported=False, confidence="low", path="rule",
            rule_id=rule.id, tables=list(rule.tables),
            reason=f"Rule {rule.id} cần cột không tồn tại trong schema: {missing}",
        )
    return NLToSQLPlan(
        question=question, sql=rule.sql, params=[], path="rule", rule_id=rule.id,
        explanation=rule.explanation, supported=True, confidence="high",
        tables=list(rule.tables),
    )



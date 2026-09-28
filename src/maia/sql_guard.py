"""Read-only SQL guard shared by the MCP SQL server and the market warehouse.

Threat model: the SQL text is *model-generated* (Text-to-SQL) or comes from an
operator console, and the connection may reach a real warehouse. The prompt is a
hint, not a security boundary — this module is the boundary.

Enforced rules, all fail-closed:

1. **One statement only.** A trailing ``;`` is tolerated (models emit it); any
   other ``;`` is rejected, so ``SELECT 1; DROP TABLE x`` cannot ride along.
2. **Read-only verbs.** The statement must start with ``SELECT`` or ``WITH`` and
   may not contain DDL/DML keywords anywhere (including inside a CTE).
3. **Table allowlist.** Every table named after ``FROM`` / ``JOIN`` must be in
   the allowlist; with ``allowed_tables=None`` the check is skipped and the plan
   records a warning, so an opt-out is visible in the audit record.
4. **Row cap.** A missing ``LIMIT`` is added and an oversized one is clamped, so
   a runaway query cannot stream a warehouse into a chat answer.
5. **Dangerous features blocked.** ``ATTACH``/``PRAGMA``/``VACUUM``/
   ``load_extension``/``readfile``/``writefile`` are rejected even in a SELECT.
6. **Comments are stripped before analysis**, so a keyword cannot hide in
   ``/* ... */`` and the analysed text equals the executed text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["SQLGuardError", "SQLPlan", "guard", "strip_comments"]

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_WHITESPACE_RE = re.compile(r"\s+")

_DENY_KEYWORDS = frozenset(
    {
        "insert", "update", "delete", "merge", "upsert", "replace",
        "drop", "alter", "create", "truncate", "rename",
        "grant", "revoke", "attach", "detach", "pragma", "vacuum", "reindex",
        "begin", "commit", "rollback", "savepoint", "release",
    }
)
_DENY_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile"})

_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_$]*(?:\.[a-zA-Z_][a-zA-Z0-9_$]*)?)",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\s*(?:\boffset\s+\d+)?\s*$", re.IGNORECASE)
_CTE_NAME_RE = re.compile(r"\bwith\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", re.IGNORECASE)


class SQLGuardError(ValueError):
    """Raised when a statement violates the read-only policy."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class SQLPlan:
    """Result of analysing a statement: what will run and what it touches."""

    sql: str
    tables: list[str] = field(default_factory=list)
    limited: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sql": self.sql,
            "tables": list(self.tables),
            "limited": self.limited,
            "warnings": list(self.warnings),
        }


def strip_comments(sql: str) -> str:
    """Remove SQL comments and collapse whitespace.

    Doing this *before* every check is what makes the guard meaningful: a denied
    keyword hidden inside a comment must not survive into the executed text.
    """
    without_block = _BLOCK_COMMENT_RE.sub(" ", sql or "")
    without_line = _LINE_COMMENT_RE.sub(" ", without_block)
    return _WHITESPACE_RE.sub(" ", without_line).strip()


def _split_statements(sql: str) -> list[str]:
    return [p.strip() for p in sql.split(";") if p.strip()]


def _check_keywords(sql: str) -> None:
    lowered = sql.lower()
    for keyword in sorted(_DENY_KEYWORDS):
        if re.search(rf"\b{keyword}\b", lowered):
            raise SQLGuardError(f"statement contains a forbidden keyword: {keyword.upper()}")
    for func in sorted(_DENY_FUNCTIONS):
        if re.search(rf"\b{func}\s*\(", lowered):
            raise SQLGuardError(f"statement calls a forbidden function: {func}()")


def _apply_limit(sql: str, max_rows: int) -> tuple[str, bool]:
    match = _LIMIT_RE.search(sql)
    if not match:
        return f"{sql} LIMIT {max_rows}", True
    if int(match.group(1)) > max_rows:
        return f"{sql[: match.start(1)]}{max_rows}{sql[match.end(1):]}", True
    return sql, False


def guard(
    sql: str,
    *,
    allowed_tables: frozenset[str] | set[str] | None = None,
    max_rows: int = 1000,
    require_from_clause: bool = True,
) -> SQLPlan:
    """Validate ``sql`` and return the bounded, comment-free statement to run."""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLGuardError("empty SQL statement")

    cleaned = strip_comments(sql)
    statements = _split_statements(cleaned)
    if len(statements) > 1:
        raise SQLGuardError(
            f"multiple statements are not allowed ({len(statements)} found)"
        )
    if not statements:
        raise SQLGuardError("empty SQL statement after comments were removed")

    statement = statements[0]
    lowered = statement.lower()
    if not lowered.startswith(("select", "with")):
        raise SQLGuardError("only SELECT / WITH (read-only) statements are allowed")

    _check_keywords(statement)

    cte_names = {m.group(1).lower() for m in _CTE_NAME_RE.finditer(statement)}
    tables = [
        ref.split(".")[-1].lower()
        for ref in _TABLE_REF_RE.findall(statement)
        if ref.split(".")[-1].lower() not in cte_names and ref.lower() not in cte_names
    ]

    warnings: list[str] = []
    if allowed_tables is None:
        warnings.append("table allowlist not enforced by caller")
    else:
        permitted = {t.lower() for t in allowed_tables}
        unknown = sorted({t for t in tables if t not in permitted})
        if unknown:
            raise SQLGuardError(
                f"statement references tables outside the allowlist: {unknown}"
            )
    if require_from_clause and not tables:
        raise SQLGuardError("statement does not read from any table")

    bounded, limited = _apply_limit(statement, max_rows)
    return SQLPlan(
        sql=bounded, tables=sorted(set(tables)), limited=limited, warnings=warnings
    )


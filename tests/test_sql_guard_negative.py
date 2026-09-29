"""
Negative tests cho SQL guard.

Bảng tấn công / hành vi mong đợi:

    ATTACK / FAILURE                    EXPECTED CONTROL
    ------------------------------------ --------------------------------------
    INSERT / UPDATE / DELETE            raise SQLGuardError
    DROP / ALTER / TRUNCATE             raise SQLGuardError
    stacked statements (;)              raise SQLGuardError
    comment-hidden keyword              raise (comment bị strip trước)
    CTE đặt tên trùng keyword           raise
    load_extension()                    raise
    bảng ngoài allowlist                raise
    subquery đọc bảng ngoài allowlist   raise
    không có FROM                       raise
    thiếu LIMIT khi max_rows            tự thêm LIMIT, không raise
    statement rỗng / chỉ comment       raise
    SQL không phải chuỗi               raise
"""
from __future__ import annotations

import pytest

from maia.sql_guard import SQLGuardError, guard


def rejects(sql, *, match: str = "", **kw) -> None:
    """Assert rằng `sql` bị từ chối. Không ngoại lệ -> test FAIL."""
    with pytest.raises(SQLGuardError, match=match or None):
        guard(sql, **kw)


# ==========================================================================
# 1. Ghi dữ liệu bị chặn
# ==========================================================================
@pytest.mark.parametrize("sql", [
    "INSERT INTO logs (msg) VALUES ('x')",
    "UPDATE logs SET msg = 'x'",
    "DELETE FROM logs",
    "MERGE INTO logs USING src ON 1=1",
    "REPLACE INTO logs VALUES (1)",
    "UPSERT INTO logs VALUES (1)",
])
def test_write_statements_rejected(sql):
    """Chỉ SELECT được phép. Ghi dữ liệu do LLM sinh là đường xâm nhập
    kinh điển: prompt injection dẫn tới SQL chèn hàng giả."""
    rejects(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE logs",
    "ALTER TABLE logs ADD COLUMN x INT",
    "CREATE TABLE t (a INT)",
    "TRUNCATE logs",
])
def test_ddl_rejected(sql):
    rejects(sql)


@pytest.mark.parametrize("sql", [
    "PRAGMA table_info(logs)",
    "VACUUM",
    "ATTACH DATABASE 'x' AS y",
])
def test_sqlite_introspection_rejected(sql):
    """PRAGMA/VACUUM/ATTACH không nằm trong deny-list từ khoá thường gặp
    nhưng vẫn là đường vòng để đọc/ghi ngoài dự kiến."""
    rejects(sql)


# ==========================================================================
# 2. Chèn nhiều statement
# ==========================================================================
def test_stacked_statements_rejected():
    """`SELECT 1; DROP TABLE logs` là cách bỏ qua kiểm tra keyword
    nếu ta chỉ nhìn statement đầu tiên."""
    rejects("SELECT 1; DROP TABLE logs", match="multiple statements")


def test_semicolon_inside_string_literal_is_not_a_separator():
    """Dấu `;` trong chuỗi KHÔNG phải ranh giới statement. Nếu coi là ranh
    giới, một truy vấn hợp lệ sẽ bị từ chối — và đó cũng là một lỗi."""
    plan = guard("SELECT id FROM logs WHERE msg = 'a;b'")


# ==========================================================================
# 3. Giấu từ khoá sau comment
# ==========================================================================
def test_keyword_hidden_after_comment_rejected():
    """Comment phải được gỡ TRƯỚC khi kiểm tra từ khoá. Kiểm tra trên
    chuỗi thô nghĩa là nội dung sau comment vẫn bị quét — và nếu
    không gỡ comment, một câu chỉ gồm comment sẽ đi lọt."""
    rejects("SELECT 1 /* x */ ; DROP TABLE logs")


def test_comment_only_statement_rejected():
    """Câu chỉ có comment không phải SQL, nhưng đưa vào engine có thể là
    lỗi parser hoặc lệch hành vi giữa các phiên bản."""
    rejects("-- just a comment")


# ==========================================================================
# 4. Lọc bảng
# ==========================================================================
def test_table_outside_allowlist_rejected():
    rejects("SELECT * FROM secrets", allowed_tables=frozenset({"logs"}))


def test_subquery_outside_allowlist_rejected():
    """Subquery là đường vòng quen thuộc: FROM ngoài cùng hợp lệ nhưng
    bên trong đọc bảng khác."""
    rejects("SELECT * FROM logs WHERE id IN (SELECT id FROM secrets)",
            allowed_tables=frozenset({"logs"}))


def test_cte_name_matching_deny_keyword_rejected():
    """CTE tên `update` không được né kiểm tra từ khoá."""
    rejects("WITH update AS (SELECT 1) SELECT * FROM logs",
            allowed_tables=frozenset({"logs"}))


def test_missing_from_clause_rejected():
    rejects("SELECT 1", require_from_clause=True)


# ==========================================================================
# 5. Giới hạn kết quả
# ==========================================================================
def test_limit_added_when_missing():
    plan = guard("SELECT * FROM logs", max_rows=100)
    assert plan.limited, "phai tu them LIMIT khi nguoi goi khong dat"


def test_existing_limit_respected():
    plan = guard("SELECT * FROM logs LIMIT 5", max_rows=100)
    assert not plan.limited
    assert "LIMIT 5" in plan.sql


def test_limit_above_max_is_lowered():
    """LIMIT 100000 do LLM sinh là một cách để kéo cả bảng về. Guard
    phải ép về trần."""
    plan = guard("SELECT * FROM logs LIMIT 100000", max_rows=100)
    assert "100000" not in plan.sql, "nguoi goi khong duoc vuot tran max_rows"


# ==========================================================================
# 6. Hàm nguy hiểm và input
# ==========================================================================
def test_load_extension_rejected():
    rejects("SELECT load_extension('evil.so') FROM logs")


def test_empty_and_whitespace_rejected():
    rejects("")
    rejects("   \n\t  ")


def test_non_string_rejected():
    """Truyền object thay vì chuỗi phải ra lỗi rõ ràng, không phải
    AttributeError kiểu 500."""
    rejects(None)
    rejects(123)
    rejects(["SELECT 1"])


# ==========================================================================
# 7. Truy vấn hợp lệ phải đi qua
# ==========================================================================
def test_legitimate_select_allowed():
    plan = guard("SELECT id, msg FROM logs WHERE ts > 0",
                 allowed_tables=frozenset({"logs"}), max_rows=50)
    assert "logs" in plan.tables
    assert "LIMIT" in plan.sql.upper()


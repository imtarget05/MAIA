"""Migration proofs M1/M3/M4 and the schema negative control (A2/A3).

Why these are tests and not a checklist: a migration suite that has only ever
seen a happy path cannot tell a working history from one that silently records
success. Every proof here is designed so that a broken implementation makes it
RED.

Isolation: each proof runs against a dedicated throwaway DATABASE on the same
PostgreSQL service, created and dropped by the test. Using a separate database
rather than the shared `maia_test` keeps a deliberately-failed migration from
leaving the schema the rest of the suite depends on in a half-applied state.

  M1  empty -> head, revision recorded, every durable table present
  M3  head -> head again, clean, nothing duplicated
  M4  a deliberately broken revision FAILS, and `alembic_version` is NOT
      stamped with the target revision  <- the "false head" failure
  A3  remove a load-bearing tenant constraint from the migrated schema and the
      schema contract assertion FAILS for exactly that constraint
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_DSN = os.environ.get(
    "MAIA_TEST_ADMIN_DSN", "postgresql://maia:maia@localhost:5432/maia_test"
)
HEAD = "3_durable_shared_state"

DURABLE_TABLES = {
    "conversations",
    "conversation_messages",
    "agent_runs",
    "agent_checkpoints",
    "approval_requests",
    "idempotency_records",
    "audit_events",
    "job_records",
}

# Load-bearing constraint for A3: without it, two tenants could collide on the
# same session_id, and the existence of one tenant's session would leak through
# an insert error.
LOAD_BEARING_CONSTRAINT = "uq_conversations_tenant_session"

pytestmark = pytest.mark.skipif(
    not (
        importlib.util.find_spec("psycopg") is not None
        and importlib.util.find_spec("alembic") is not None
    ),
    reason="psycopg + alembic required (requirements.txt)",
)


def _run_alembic(
    args: list[str], dsn: str, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MAIA_DATABASE_URL"] = dsn
    # sys.executable, not "python": the interpreter running the test is the one
    # that has alembic installed. Hardcoding "python" failed locally with
    # FileNotFoundError and would fail again in any environment where the bare
    # name is absent.
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.fixture
def probe_db():
    """A throwaway database, created then destroyed."""
    import psycopg

    name = f"maia_mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=10) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield dsn
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=10) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _current_revision(dsn: str) -> str | None:
    """The stamped revision, or None if nothing is recorded.

    None covers the case that matters most for M4: PostgreSQL runs Alembic with
    transactional DDL, so a migration that fails mid-way rolls back the version
    table too. "The table does not exist" and "no revision is stamped" mean the
    same thing — nothing was recorded — and treating the missing table as an
    error would have turned the correct outcome into a failing test.
    """
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        try:
            rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        except psycopg.errors.UndefinedTable:
            return None
    return rows[0][0] if rows else None


def _tables(dsn: str) -> set[str]:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# M1 / M3
# ---------------------------------------------------------------------------


def test_M1_empty_database_upgrades_to_head(probe_db: str) -> None:
    result = _run_alembic(["upgrade", "head"], probe_db)
    assert result.returncode == 0, f"upgrade failed:\n{result.stdout}\n{result.stderr}"

    assert _current_revision(probe_db) == HEAD, (
        "alembic_version does not record the head — the database would be "
        "indistinguishable from one that was never migrated"
    )
    missing = DURABLE_TABLES - _tables(probe_db)
    assert not missing, f"head revision did not create: {sorted(missing)}"


def test_M3_upgrading_at_head_is_a_clean_no_op(probe_db: str) -> None:
    first = _run_alembic(["upgrade", "head"], probe_db)
    assert first.returncode == 0, first.stderr
    before = _tables(probe_db)

    second = _run_alembic(["upgrade", "head"], probe_db)
    assert second.returncode == 0, (
        f"re-running upgrade at head failed:\n{second.stdout}\n{second.stderr}"
    )
    assert _tables(probe_db) == before, "a second upgrade changed the schema"
    assert _current_revision(probe_db) == HEAD


# ---------------------------------------------------------------------------
# M4 — a broken migration must fail AND must not be recorded as applied
# ---------------------------------------------------------------------------

BROKEN_REVISION = '''
"""deliberately broken revision, used only by the M4 proof.

Generated into a temporary versions directory by tests/test_migration_proofs.py
and destroyed with it. Never merge this.
"""
import sqlalchemy as sa
from alembic import op

revision = "99_broken_probe"
down_revision = "3_durable_shared_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Real DDL first, so the failure is a genuine SQL error part-way through
    # rather than a syntax error in the file. That distinction matters: a syntax
    # failure never touches the database and so never exercises the "was this
    # revision recorded?" question the proof exists for.
    op.create_table("probe_table", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("SELECT this_function_does_not_exist()")


def downgrade() -> None:
    op.drop_table("probe_table")
'''


def _versions_dir_with_broken(tmp_path) -> Path:
    """A copy of the real revision chain plus one deliberately broken revision."""
    versions = tmp_path / "versions"
    versions.mkdir()
    real = REPO_ROOT / "alembic" / "versions"
    for f in real.glob("*.py"):
        shutil.copy2(f, versions / f.name)
    (versions / "99_broken_probe.py").write_text(BROKEN_REVISION, encoding="utf-8")
    return versions


def test_M4_broken_migration_fails_and_is_not_stamped(probe_db: str, tmp_path) -> None:
    """The target revision must not appear in alembic_version after a failure.

    This is the proof that a failed migration is visible as a failure. If the
    broken revision were recorded anyway, every later `upgrade head` becomes a
    no-op and the missing table resurfaces much later as an unrelated
    "relation does not exist" — which is exactly the CI failure this repository
    already hit once when the durable suite ran against an unmigrated database.
    """
    versions = _versions_dir_with_broken(tmp_path)

    ini = tmp_path / "alembic.ini"
    shutil.copy2(REPO_ROOT / "alembic.ini", ini)
    ini.write_text(
        ini.read_text(encoding="utf-8").replace(
            "script_location = alembic",
            f"script_location = {REPO_ROOT / 'alembic'}\n"
            f"version_locations = {versions}",
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "MAIA_DATABASE_URL": probe_db},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode != 0, (
        "a migration executing a nonexistent SQL function SUCCEEDED — either the "
        "database accepted an impossible statement or this proof is not measuring "
        "what it claims"
    )

    stamped = _current_revision(probe_db)
    assert stamped != "99_broken_probe", (
        "the broken revision was recorded in alembic_version as applied, so every "
        "later `upgrade head` would skip it"
    )
    # The rest of the assertion was originally `== HEAD`, written on the
    # assumption that a failed upgrade leaves the earlier revisions applied.
    # PostgreSQL runs Alembic with transactional DDL, so the whole upgrade rolls
    # back: nothing is stamped at all. That is STRONGER than partial persistence,
    # so the assertion records the fact rather than the guess.
    assert stamped in (None, HEAD), (
        f"expected no revision or the pre-existing head after rollback, "
        f"found {stamped!r} — the database is in a state no later upgrade can trust"
    )


# ---------------------------------------------------------------------------
# A3 — schema negative control
# ---------------------------------------------------------------------------


def _has_constraint(dsn: str, name: str) -> bool:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = %s", (name,)
            ).fetchone()
            is not None
        )


def test_A3_removing_the_tenant_constraint_is_detected(probe_db: str) -> None:
    """Drop the load-bearing unique constraint and require the check to notice.

    This separates "the migration creates a tenant-scoped unique" as a CLAIM from
    "it is present in the database" as a FACT. Without it, a regression that
    quietly drops `uq_conversations_tenant_session` ships with every other test
    still green.
    """
    result = _run_alembic(["upgrade", "head"], probe_db)
    assert result.returncode == 0, result.stderr

    # Stated positively first: the constraint must be there on a correct
    # migration, otherwise the negative half below proves nothing.
    assert _has_constraint(probe_db, LOAD_BEARING_CONSTRAINT), (
        "the baseline schema lacks the constraint this negative control removes — "
        "the control would be vacuous"
    )

    import psycopg

    with psycopg.connect(probe_db, autocommit=True) as conn:
        conn.execute(
            f'ALTER TABLE conversations DROP CONSTRAINT "{LOAD_BEARING_CONSTRAINT}"'
        )

    assert not _has_constraint(probe_db, LOAD_BEARING_CONSTRAINT), (
        "the DROP did not take effect, so this negative control proves nothing"
    )

    # The semantic consequence, not merely the catalogue: with the constraint
    # gone, two tenants collide on one session_id — exactly what it prevented.
    with psycopg.connect(probe_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO conversations (id, tenant_id, session_id, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 'a', 'shared', now(), now())"
        )
        conn.execute(
            "INSERT INTO conversations (id, tenant_id, session_id, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 'b', 'shared', now(), now())"
        )
        count = conn.execute(
            "SELECT count(*) FROM conversations WHERE session_id = 'shared'"
        ).fetchone()[0]
    assert count == 2, (
        "expected the cross-tenant collision the removed constraint existed to stop"
    )

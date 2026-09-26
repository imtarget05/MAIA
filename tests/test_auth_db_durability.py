"""B1 - the auth database DSN must be configurable and durable in deployment.

Regression: ``maia/api.py`` hardcoded ``SQLALCHEMY_DATABASE_URL =
"sqlite:///./maia_auth.db"`` with no settings field and no env override, and
``maia_auth.db`` appeared nowhere in ``render.yaml``. On a platform with an
ephemeral filesystem every user, session, refresh token and approval was
therefore discarded on each redeploy, with no error and no warning.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from maia.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
DEFAULT_AUTH_DB_URL = "sqlite:///./maia_auth.db"


def test_auth_db_url_has_a_settings_field():
    """The DSN is no longer a bare literal in api.py."""
    assert "AUTH_DB_URL" in Settings.model_fields
    assert Settings(_env_file=None).AUTH_DB_URL == DEFAULT_AUTH_DB_URL


def test_auth_db_url_local_default_is_sqlite():
    """Local development keeps the SQLite file it always had."""
    s = Settings(_env_file=None)
    assert s.AUTH_DB_URL.startswith("sqlite:///")


def test_auth_db_url_env_override(monkeypatch):
    monkeypatch.setenv("AUTH_DB_URL", "sqlite:////mnt/data/maia_auth.db")
    s = Settings(_env_file=None)
    assert s.AUTH_DB_URL == "sqlite:////mnt/data/maia_auth.db"


def test_api_derives_its_engine_url_from_settings():
    """api.py must not carry its own hardcoded DSN."""
    import maia.api as api
    from maia.config import settings

    assert api.SQLALCHEMY_DATABASE_URL == settings.AUTH_DB_URL
    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "sqlite:///./maia_auth.db" not in source


def test_render_yaml_prompts_for_auth_db_url():
    """Both services must surface the key so it is set in the dashboard."""
    text = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert text.count("key: AUTH_DB_URL") == 2
    assert "maia_auth.db" in text  # documented in the comment


def _import_config_in_subprocess(env_overrides: dict[str, str]):
    """Import maia.config in a clean interpreter.

    The startup assertion runs at module import, and reloading maia.config
    in-process would swap the `settings` singleton that other modules already
    bound with `from ..config import settings`.
    """
    env = dict(os.environ)
    env.pop("AUTH_DB_URL", None)
    env["PYTHONPATH"] = str(SRC)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent("import maia.config")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def test_startup_assertion_fails_outside_development_on_default_dsn():
    """The whole point: a deployment must not boot onto an ephemeral DB."""
    env = {"ENVIRONMENT": "production", "AUTH_DB_URL": DEFAULT_AUTH_DB_URL}
    proc = _import_config_in_subprocess(env)
    assert proc.returncode != 0, "expected a startup failure"
    assert "AUTH_DB_URL" in proc.stderr


def test_startup_assertion_passes_with_a_configured_dsn():
    env = {"ENVIRONMENT": "production", "AUTH_DB_URL": "sqlite:////mnt/data/maia_auth.db"}
    proc = _import_config_in_subprocess(env)
    assert proc.returncode == 0, proc.stderr


def test_startup_assertion_allows_postgres_dsn():
    env = {"ENVIRONMENT": "production", "AUTH_DB_URL": "postgresql://u:p@host:5432/maia_auth"}
    proc = _import_config_in_subprocess(env)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("env_name", ["development", "dev", "local", "test", ""])
def test_startup_assertion_allows_default_dsn_in_development(env_name):
    """Local development is unaffected by the assertion."""
    env = {"ENVIRONMENT": env_name, "AUTH_DB_URL": DEFAULT_AUTH_DB_URL}
    proc = _import_config_in_subprocess(env)
    assert proc.returncode == 0, proc.stderr
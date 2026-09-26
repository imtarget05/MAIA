"""B2 - the tenant-ownership check: behaviour and documentation must agree.

``_authorize_employee`` was documented as a "fail-closed tenant ownership
check" while allowing two cases outright: an employee unknown to the mock HR DB,
and an employee carrying no ``tenant_id``. The docstring now describes exactly
what the function does, and these tests pin both the guarantee it really
provides and the allowances it really makes, so the two cannot drift apart
again silently.
"""
import json

import pytest

from maia.agent.tools import (
    _authorize_employee,
    check_leave_balance,
    create_leave_request,
    set_employee_tenant,
)
from maia.config import settings


@pytest.fixture()
def hr_db(tmp_path, monkeypatch):
    """Point the mock HR DB at an isolated file and restore it afterwards."""
    path = tmp_path / "hr_mock.json"
    monkeypatch.setattr(settings, "HR_MOCK_DB_PATH", str(path))
    # Other test modules assign settings.TOOL_TENANT_CHECK = False on the shared
    # singleton without restoring it, so the ambient value depends on test
    # order. Pin it here (monkeypatch restores it) rather than assume it.
    monkeypatch.setattr(settings, "TOOL_TENANT_CHECK", True)
    return path


def test_tenant_check_is_enabled_by_default():
    """config.py documents TOOL_TENANT_CHECK as True; assert it on a fresh
    Settings rather than the (test-order dependent) shared singleton."""
    from maia.config import Settings

    assert Settings(_env_file=None).TOOL_TENANT_CHECK is True


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- the guarantee: a known employee cannot act for another tenant ---------


def test_known_employee_cannot_act_for_another_tenant(hr_db):
    _write(hr_db, {"emp_001": {"balance": 10, "requests": [], "tenant_id": "acme"}})
    ok, reason = _authorize_employee("emp_001", "evil-corp")
    assert ok is False
    assert "unauthorized" in reason


def test_known_employee_is_allowed_for_its_own_tenant(hr_db):
    _write(hr_db, {"emp_001": {"balance": 10, "requests": [], "tenant_id": "acme"}})
    assert _authorize_employee("emp_001", "acme") == (True, None)


def test_cross_tenant_leave_request_is_refused_and_writes_nothing(hr_db):
    _write(hr_db, {"emp_001": {"balance": 10, "requests": [], "tenant_id": "acme"}})
    result = create_leave_request("emp_001", days=1, tenant_id="evil-corp")
    assert result["ok"] is False
    assert result["error"] == "unauthorized: employee not in tenant"
    # The victim's balance is untouched.
    assert _read(hr_db)["emp_001"]["balance"] == 10
    assert _read(hr_db)["emp_001"]["requests"] == []


# --- the documented allowances (NOT fail-closed) ---------------------------


def test_unknown_employee_is_allowed(hr_db):
    """Documented allowance: the mock auto-provisions unknown employees."""
    _write(hr_db, {})
    assert _authorize_employee("emp_999", "acme") == (True, None)


def test_unknown_employee_is_adopted_into_the_asserted_tenant_when_persisting(hr_db):
    _write(hr_db, {})
    assert _authorize_employee("emp_999", "acme", persist_unknown=True) == (True, None)
    assert _read(hr_db)["emp_999"]["tenant_id"] == "acme"
    # ...and is bound from then on.
    ok, _ = _authorize_employee("emp_999", "other-corp")
    assert ok is False


def test_employee_without_tenant_is_allowed_and_adopted(hr_db):
    _write(hr_db, {"emp_002": {"balance": 5, "requests": []}})
    assert _authorize_employee("emp_002", "acme") == (True, None)
    assert _authorize_employee("emp_002", "acme", persist_unknown=True) == (True, None)
    assert _read(hr_db)["emp_002"]["tenant_id"] == "acme"


def test_no_tenant_asserted_is_allowed(hr_db):
    """Nothing to compare against, so the check is skipped."""
    _write(hr_db, {"emp_001": {"balance": 10, "requests": [], "tenant_id": "acme"}})
    assert _authorize_employee("emp_001", None) == (True, None)


def test_check_disabled_is_allowed(hr_db, monkeypatch):
    _write(hr_db, {"emp_001": {"balance": 10, "requests": [], "tenant_id": "acme"}})
    monkeypatch.setattr(settings, "TOOL_TENANT_CHECK", False)
    assert _authorize_employee("emp_001", "evil-corp") == (True, None)


# --- the docstring must keep matching the code -----------------------------


def test_docstring_does_not_claim_fail_closed():
    """The original defect, locked shut.

    The docstring may mention the phrase only in order to deny it; what it must
    never do is assert the property. It must also name every allow-path the
    body has, so a future behaviour change cannot leave the docs quietly behind.
    """
    import re

    doc = _authorize_employee.__doc__ or ""
    matches = list(re.finditer(r"fail-?closed", doc, re.IGNORECASE))
    assert matches, "docstring should address the fail-closed question head-on"
    for m in matches:
        before = doc[max(0, m.start() - 40):m.start()].lower()
        assert re.search(r"not\b", before), (
            "docstring asserts fail-closed, but the function allows unknown "
            "employees and tenant-less records"
        )
    assert "tenant-mismatch" in doc.lower()
    for case in ("unknown", "no ``tenant_id``", "TOOL_TENANT_CHECK", "denied"):
        assert case in doc, "docstring does not mention %r" % (case,)


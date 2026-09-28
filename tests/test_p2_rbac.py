"""P2: RBAC enforcement — non-admin 403, inactive rejected."""
from fastapi import HTTPException

from maia import api as api_mod
from maia.models import UserRole


class _U:
    def __init__(self, role, active=True):
        self.role = role
        self.is_active = active


def test_non_admin_forbidden():
    try:
        api_mod.get_current_admin_user(_U(UserRole.USER))
        raise AssertionError("expected 403")
    except HTTPException as e:
        assert e.status_code == 403


def test_inactive_rejected():
    try:
        api_mod.get_current_active_user(_U(UserRole.ADMIN, active=False))
        raise AssertionError("expected 4xx")
    except HTTPException as e:
        assert e.status_code in (400, 401, 403)


def test_admin_passes():
    u = _U(UserRole.ADMIN)
    assert api_mod.get_current_admin_user(u) is u

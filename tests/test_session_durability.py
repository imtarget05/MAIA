"""P0 regression: SessionStore must NOT wipe rows on new instance (durability)."""
from maia.agent.session import SessionStore


def test_session_survives_new_instance(tmp_path):
    db = str(tmp_path / "sess.db")
    s1 = SessionStore(db_path=db)
    s1.append("s1", "user", "xin nghỉ 2 ngày", intent="leave_request", tenant_id="t1")
    # New instance on same DB file must see history (no DELETE in _init_schema).
    s2 = SessionStore(db_path=db)
    hist = s2.history("s1", tenant_id="t1")
    assert len(hist) == 1 and hist[0]["content"] == "xin nghỉ 2 ngày"

    s1.set_pending("s1", {"tool": "create_leave_request", "params": {"days": 2},
                          "summary": "leave", "question": "q"}, tenant_id="t1")
    s3 = SessionStore(db_path=db)
    assert s3.get_pending("s1", tenant_id="t1")["tool"] == "create_leave_request"

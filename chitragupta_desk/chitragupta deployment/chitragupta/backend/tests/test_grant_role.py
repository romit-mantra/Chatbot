"""Governed role granting — the most security-sensitive write.
Both STAGING and APPROVAL require System Manager. Full before/after audit."""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from erp import FakeERP
from store import make_session, AuditLog
from engine import Engine
from planner import Planner, ScriptedClient
from governance import USERS


def mk(tmp_path, responses):
    erp = FakeERP()
    erp.store["User"] = {"priya@m.com": {"name": "priya@m.com",
                                         "roles": [{"role": "Employee"}]}}
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(erp, make_session(db),
                  planner=Planner(ScriptedClient(responses)))


GRANT = json.dumps({"op": "grant_role",
                    "doc": {"user": "priya@m.com", "role": "Purchase User"}})


def test_sm_can_stage_and_approve_grant(tmp_path):
    USERS["admin1"] = {"full_name": "Admin", "roles": ["System Manager"]}
    try:
        eng = mk(tmp_path, [GRANT])
        r = eng.handle_command("grant Purchase User to priya@m.com", "admin1")
        assert r["proposals"] and "Purchase User" in r["reply"]
        # not applied yet
        roles = [x["role"] for x in eng.erp.get("User", "priya@m.com")["roles"]]
        assert "Purchase User" not in roles
        out = eng.approve(r["proposals"][0]["id"], "admin1")
        assert out["readback_ok"] is True
        roles = [x["role"] for x in eng.erp.get("User", "priya@m.com")["roles"]]
        assert "Purchase User" in roles and "Employee" in roles
        s = eng.Session()
        ev = [a for a in s.query(AuditLog).all() if a.event == "role_granted"]
        assert ev and "roles_before" in ev[0].detail_json
    finally:
        USERS.pop("admin1", None)


def test_non_sm_cannot_even_stage(tmp_path):
    eng = mk(tmp_path, [GRANT])
    r = eng.handle_command("grant Purchase User to priya@m.com", "ravi")
    assert r["proposals"] == []
    assert "restricted to" in r["reply"] and "System Manager" in r["reply"]


def test_non_sm_cannot_approve_even_if_staged(tmp_path):
    USERS["admin1"] = {"full_name": "Admin", "roles": ["System Manager"]}
    try:
        eng = mk(tmp_path, [GRANT])
        r = eng.handle_command("grant role", "admin1")
        out = eng.approve(r["proposals"][0]["id"], "ravi")   # a non-SM approver
        # either gate may fire first: the generic RBAC submit gate or the
        # SM-only grant gate — both are correct denials (defence-in-depth)
        assert "error" in out
        assert ("System Manager" in out["error"]
                or "lacks submit permission" in out["error"])
        roles = [x["role"] for x in eng.erp.get("User", "priya@m.com")["roles"]]
        assert "Purchase User" not in roles                  # nothing applied
    finally:
        USERS.pop("admin1", None)


def test_already_has_role_is_a_noop(tmp_path):
    USERS["admin1"] = {"full_name": "Admin", "roles": ["System Manager"]}
    try:
        eng = mk(tmp_path, [json.dumps({"op": "grant_role",
                 "doc": {"user": "priya@m.com", "role": "Employee"}})])
        r = eng.handle_command("grant Employee to priya", "admin1")
        assert r["proposals"] == [] and "already has" in r["reply"]
    finally:
        USERS.pop("admin1", None)

"""Validation suite. Offline: FakeERP + file-free SQLite per test.
Covers governance tiers, RBAC per user, the full loop, approve-with-edits
learning, permission-scoped memory recall, screen context, the daily brief,
skill tracking, and the audit trail."""

import os, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from erp import FakeERP
from store import make_session, AuditLog
from engine import Engine
from governance import classify, Tier, user_can


@pytest.fixture
def engine(tmp_path):
    db = f"sqlite:///{tmp_path}/{uuid.uuid4().hex}.db"
    return Engine(FakeERP(), make_session(db))


# ---------------- governance tiers ----------------

def test_tiers():
    assert classify("Item", "get")[0] == Tier.AUTO
    assert classify("Purchase Order", "create_draft")[0] == Tier.AUTO
    assert classify("Purchase Order", "submit", 74400)[0] == Tier.APPROVE
    assert classify("Payment Entry", "submit", 5000)[0] == Tier.APPROVE
    assert classify("GL Entry", "delete")[0] == Tier.BLOCK
    assert classify("Purchase Order", "submit", 2_000_000)[0] == Tier.BLOCK


# ---------------- RBAC ----------------

def test_rbac_matrix():
    """WRITES are gated in-engine. READS are delegated to ERPNext (which 403s
    if the user really may not read something) — so user_can(..., 'read') is
    permissive here by design."""
    assert user_can("ravi", "Purchase Order", "submit")
    assert not user_can("meera", "Purchase Order", "submit")   # sales can't submit POs
    assert user_can("meera", "Sales Order", "submit")
    assert user_can("priya", "Payment Entry", "submit")
    assert not user_can("ravi", "Payment Entry", "submit")
    # reads pass this layer; ERPNext is the authority
    assert user_can("ravi", "Company", "get")
    assert user_can("meera", "Company", "get")

def test_rbac_blocks_command(engine):
    r = engine.handle_command("raise a PO for the screw", "meera")
    assert r["proposals"] == []
    assert "role" in r["reply"].lower()

def test_rbac_blocks_approval(engine):
    r = engine.handle_command("raise a PO for the screw", "ravi")
    pid = r["proposals"][0]["id"]
    out = engine.approve(pid, "meera")           # meera may not approve POs
    assert "error" in out

def test_unknown_user_rejected(engine):
    r = engine.handle_command("anything", "mallory")
    assert "error" in r


# ---------------- the loop ----------------

def test_read_is_auto(engine):
    r = engine.handle_command("how much stock of M6 hex screw?", "ravi")
    assert "4200" in r["reply"] and r["proposals"] == []

def test_action_stages_not_submits(engine):
    r = engine.handle_command("raise a PO for the screw", "ravi")
    assert r["proposals"][0]["tier"] == "approve"
    assert all(p["docstatus"] == 0 or p["name"].startswith("PUR-2026-00031")
               for p in engine.erp.search("Purchase Order"))

def test_approve_commits_and_reads_back(engine):
    r = engine.handle_command("reorder M6 hex screw", "ravi")
    out = engine.approve(r["proposals"][0]["id"], "ravi")
    assert out["readback_ok"] is True and out["status"] == "executed"
    assert out["submitted"].startswith("PUR-2026-")

def test_reject_writes_nothing(engine):
    r = engine.handle_command("raise a PO for the screw", "ravi")
    engine.reject(r["proposals"][0]["id"], "ravi")
    live = [p for p in engine.erp.search("Purchase Order")
            if p["docstatus"] == 1 and p["name"] != "PUR-2026-00031"]
    assert live == []

def test_double_approve_refused(engine):
    r = engine.handle_command("reorder the screw", "ravi")
    pid = r["proposals"][0]["id"]
    engine.approve(pid, "ravi")
    assert "error" in engine.approve(pid, "ravi")

def test_gl_delete_blocked(engine):
    r = engine.handle_command("delete the GL entry for January", "admin")
    assert r["proposals"] == [] and "block" in r["reply"].lower() \
        or "can't" in r["reply"].lower()


# ---------------- approve-with-edits + learning ----------------

def test_edit_then_approve_recalcs_and_learns(engine):
    r = engine.handle_command("raise a PO for the screw", "ravi")
    pid = r["proposals"][0]["id"]
    out = engine.approve(pid, "ravi", edits={"rate": 12.10})
    assert out["readback_ok"] is True
    sub = engine.erp.get("Purchase Order", out["submitted"])
    assert sub["rate"] == 12.10
    assert sub["total"] == round(sub["qty"] * 12.10, 2)     # total recalculated
    assert any("learned" in n for n in out["learned"])       # the diff was learned

def test_clean_vs_edited_skill_tracking(engine):
    r1 = engine.handle_command("raise a PO for the screw", "ravi")
    engine.approve(r1["proposals"][0]["id"], "ravi")                       # clean
    r2 = engine.handle_command("reorder the M8 bolt", "ravi")
    engine.approve(r2["proposals"][0]["id"], "ravi", edits={"qty": 3000})  # edited
    sk = engine.memory.skills()[0]
    assert sk["runs"] == 2 and sk["approved_clean"] == 1
    assert sk["clean_rate"] == 0.5
    assert sk["vetted"] is False                     # vetting stays manual


# ---------------- memory ----------------

def test_explicit_remember_and_use(engine):
    engine.handle_command("remember default_warehouse Unit-2", "ravi")
    r = engine.handle_command("raise a PO for the screw", "ravi")
    assert r["proposals"][0]["fields"].get("warehouse") == "Unit-2"

def test_memory_is_per_user(engine):
    engine.memory.remember_personal("ravi", "default_warehouse", "Unit-2")
    assert engine.memory.recall("priya")["personal"] == {}   # never crosses users

def test_org_memory_recall_is_permission_scoped(engine):
    # learned about Payment Entry -> only visible to users who can READ it
    engine.memory.remember_org("priya", "Payment Entry", "acme_terms", "net-15")
    assert "acme_terms" in engine.memory.recall("priya")["org"]     # accounts mgr
    assert "acme_terms" not in engine.memory.recall("meera")["org"]  # sales user

def test_memory_visible_and_deletable_only_by_owner(engine):
    engine.memory.remember_personal("ravi", "units", "lakhs")
    rows = engine.memory.list_for_user("ravi")
    assert rows and rows[0]["key"] == "units"
    assert engine.memory.forget("priya", rows[0]["id"]) is False    # not hers
    assert engine.memory.forget("ravi", rows[0]["id"]) is True

def test_contradiction_lowers_confidence(engine):
    engine.memory.remember_org("priya", "Purchase Order", "acme_terms", "net-30")
    engine.memory.remember_org("priya", "Purchase Order", "acme_terms", "net-15")
    s = engine.Session()
    from store import Memory
    m = s.query(Memory).filter_by(kind="org", key="acme_terms").first()
    assert m.value == "net-15" and m.confidence < 0.8

def test_repetition_triggers_suggestion(engine):
    assert engine.memory.track_correction("ravi", "warehouse", "Unit-2") is None
    assert engine.memory.track_correction("ravi", "warehouse", "Unit-2") is None
    assert "remember" in engine.memory.track_correction("ravi", "warehouse", "Unit-2")


# ---------------- screen context ----------------

def test_context_aware_this(engine):
    r = engine.handle_command("what's the status of this?", "ravi",
                              context={"doctype": "Purchase Order",
                                       "name": "PUR-2026-00031"})
    assert "PUR-2026-00031" in r["reply"] and "To Receive" in r["reply"]

def test_context_read_is_delegated_to_erp(engine):
    """Reads are no longer refused by our map — ERPNext decides. Here FakeERP
    allows it, so the read succeeds."""
    r = engine.handle_command("tell me about this", "meera",
                              context={"doctype": "GL Entry", "name": "GL-2026-00001"})
    assert r["proposals"] == []


# ---------------- daily brief ----------------

def test_brief_is_permission_scoped(engine):
    engine.handle_command("raise a PO for the screw", "ravi")   # 1 pending approval
    b = engine.brief("ravi")
    joined = " ".join(b["items"])
    assert "approval" in joined and "low stock" in joined
    b2 = engine.brief("meera")            # sales user: no PO approvals for her
    assert "approval" not in " ".join(b2["items"])


# ---------------- audit ----------------

def test_audit_full_chain(engine):
    r = engine.handle_command("reorder M6 hex screw", "ravi")
    engine.approve(r["proposals"][0]["id"], "ravi", edits={"rate": 12.0})
    s = engine.Session()
    events = {a.event for a in s.query(AuditLog).all()}
    assert {"proposal_staged", "approved", "committed", "memory_stored"} <= events

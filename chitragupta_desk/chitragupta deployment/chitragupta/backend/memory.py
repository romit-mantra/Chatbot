"""Memory service — the learning layer (docs/05-memory.md).

Rules enforced here:
1. Recall is permission-aware: personal memory only for its owner; org memory
   only if the current user can READ the doctype it was learned about.
2. Learning from outcomes: approval reinforces the facts in a draft; a human
   EDIT before approval stores the corrected value as the new org norm.
3. Repetition -> suggestion: the 3rd time a user overrides the same field the
   engine SUGGESTS saving it (confirm-before-store; nothing silent).
4. Skills: each approved run increments counters; 'vetted' stays manual.
"""

from __future__ import annotations

from store import Memory, Skill, audit
from governance import can_see_memory

SUGGEST_AFTER = 3  # identical corrections before we offer to remember


class MemoryService:
    def __init__(self, Session):
        self.Session = Session
        self._correction_counts: dict[tuple, int] = {}

    # ---------- store ----------
    def remember_personal(self, user: str, key: str, value: str, source="explicit"):
        s = self.Session()
        row = (s.query(Memory).filter_by(kind="personal", owner=user, key=key).first())
        if row:
            row.value, row.source, row.confidence = value, source, 1.0
        else:
            s.add(Memory(kind="personal", owner=user, key=key, value=value, source=source))
        s.commit()
        audit(s, "memory_stored", user, kind="personal", key=key)

    def remember_org(self, actor: str, doctype: str, key: str, value: str, source="learned"):
        s = self.Session()
        row = (s.query(Memory).filter_by(kind="org", scope_doctype=doctype, key=key).first())
        if row:
            if row.value != value:      # contradiction -> replace but drop confidence
                row.value, row.confidence, row.source = value, 0.6, source
            else:                       # reinforcement
                row.confidence = min(1.0, row.confidence + 0.1)
        else:
            s.add(Memory(kind="org", scope_doctype=doctype, key=key,
                         value=value, source=source, confidence=0.8))
        s.commit()
        audit(s, "memory_stored", actor, kind="org", doctype=doctype, key=key)

    # ---------- recall (permission-scoped) ----------
    def recall(self, user: str, doctype: str | None = None) -> dict:
        s = self.Session()
        personal = {m.key: m.value for m in
                    s.query(Memory).filter_by(kind="personal", owner=user).all()}
        org: dict[str, str] = {}
        q = s.query(Memory).filter_by(kind="org")
        if doctype:
            q = q.filter_by(scope_doctype=doctype)
        for m in q.all():
            # org memory only surfaces if the user's ROLE covers that doctype.
            # NOTE: this uses its own check, NOT user_can(..., "read") — reads are
            # now delegated to ERPNext (permissive here), but MEMORY must stay
            # strictly scoped or it becomes a permission-bypass channel.
            if can_see_memory(user, m.scope_doctype):
                org[m.key] = m.value
        return {"personal": personal, "org": org}

    def list_for_user(self, user: str) -> list[dict]:
        """The 'what you've taught me' page: visible + deletable."""
        s = self.Session()
        return [{"id": m.id, "key": m.key, "value": m.value, "source": m.source}
                for m in s.query(Memory).filter_by(kind="personal", owner=user).all()]

    def forget(self, user: str, memory_id: int) -> bool:
        s = self.Session()
        m = s.get(Memory, memory_id)
        if not m or m.kind != "personal" or m.owner != user:
            return False   # you can only delete YOUR OWN personal memory
        s.delete(m); s.commit()
        audit(s, "memory_forgotten", user, key=m.key)
        return True

    # ---------- learning hooks ----------
    def on_approved(self, actor: str, doctype: str, payload: dict, edited: bool,
                    original: dict | None = None) -> list[str]:
        """Called by the engine when a proposal is approved. Returns notes."""
        notes = []
        if edited and original:
            for k, new_v in payload.items():
                old_v = original.get(k)
                # NOTE: old_v may be None when the human ADDED a field in their
                # edit — that is exactly the kind of correction worth learning.
                if old_v != new_v and k not in ("name", "docstatus", "items"):
                    self.remember_org(actor, doctype, f"preferred_{k}", str(new_v))
                    notes.append(f"learned: {doctype}.{k} -> {new_v} (from your edit)")
        else:
            # clean approval reinforces the vendor/rate norms embedded in it
            if "supplier" in payload and "rate" in payload:
                self.remember_org(actor, doctype,
                                  f"rate::{payload.get('item','')}::{payload['supplier']}",
                                  str(payload["rate"]))
        return notes

    def track_correction(self, user: str, field: str, value: str) -> str | None:
        """Repetition detector: after N identical corrections, offer to remember."""
        k = (user, field, value)
        self._correction_counts[k] = self._correction_counts.get(k, 0) + 1
        if self._correction_counts[k] == SUGGEST_AFTER:
            return (f"You've set {field} = {value} {SUGGEST_AFTER} times — "
                    f"want me to make it your default? (say: remember {field} {value})")
        return None


    # ---------- retrieval of what has been learned ----------
    def org_norms(self, doctype: str) -> dict:
        """Norms learned for a doctype (from human edits on approval)."""
        s = self.Session()
        rows = s.query(Memory).filter_by(kind="org", scope_doctype=doctype).all()
        return {m.key: m.value for m in rows}

    def precedents(self, doctype: str, party: str | None = None) -> dict:
        """Rates/values learned from previously approved documents."""
        s = self.Session()
        rows = s.query(Memory).filter_by(kind="org", scope_doctype=doctype).all()
        out = {}
        for m in rows:
            if m.key.startswith("rate::"):
                _, item, sup = (m.key.split("::") + ["", ""])[:3]
                if party and party.lower() not in sup.lower():
                    continue
                out[item or sup] = m.value
        return out

    def learning_stats(self) -> dict:
        """The 'is it actually learning?' numbers — chart clean_rate weekly."""
        from store import AuditLog
        s = self.Session()
        approvals = s.query(AuditLog).filter_by(event="approved").all()
        total = len(approvals)
        clean = 0
        for a in approvals:
            try:
                import json as _j
                if _j.loads(a.detail_json or "{}").get("edited") is False:
                    clean += 1
            except Exception:
                pass
        norms = s.query(Memory).filter_by(kind="org").count()
        prefs = s.query(Memory).filter_by(kind="personal").count()
        return {
            "approvals_total": total,
            "approved_without_edits": clean,
            "clean_rate": round(clean / total, 3) if total else None,
            "org_norms_learned": norms,
            "personal_preferences": prefs,
            "skills": self.skills(),
        }

    # ---------- skills ----------
    def skill_run(self, name: str, doctype: str, clean: bool):
        s = self.Session()
        sk = s.query(Skill).filter_by(name=name).first()
        if not sk:
            sk = Skill(name=name, doctype=doctype, runs=0, approved_clean=0,
                       vetted=False)
            s.add(sk)
        sk.runs += 1
        if clean:
            sk.approved_clean += 1
        s.commit()

    def skills(self) -> list[dict]:
        s = self.Session()
        return [{"name": k.name, "doctype": k.doctype, "runs": k.runs,
                 "approved_clean": k.approved_clean, "vetted": k.vetted,
                 "clean_rate": round(k.approved_clean / k.runs, 2) if k.runs else 0}
                for k in s.query(Skill).all()]

# 06 — Testing & Validation

## How to run
```bash
cd backend
python -m pytest tests/ -v
```
The suite is **fully offline**: FakeERP + a fresh SQLite file per test, no model
key, no network. This is deliberate — determinism is what makes the governance
guarantees testable.

## What is covered (41 tests, all passing at build time)

**Auth layer (8 tests, via the real HTTP app)** — login and wrong-password
refusal; every endpoint 401s without/with a bad token; **identity comes from
the token, not the body** (a smuggled `actor` field is ignored and RBAC applies
to the session user); approvals use token identity; memory binds to the session
user; logout invalidates; expired sessions rejected; logins audited.


**LLM interpreter (10 tests, offline via a scripted model client)** — free-form
and Hinglish phrasings route to the right intent; the intent whitelist rejects
invented intents; hallucinated item names are rejected; garbage model output
degrades gracefully to the rule interpreter; fenced JSON is parsed; and the two
containment guarantees: a 'jailbroken' model cannot bypass RBAC (Meera still
refused) and cannot bypass the gate (an action still stages, never submits).


**Governance tiers** — reads/drafts auto; PO and Payment submits need approval;
GL delete and above-hard-cap values blocked (`test_tiers`).

**RBAC** — the permission matrix per role; a Sales user is refused both
*drafting* and *approving* a Purchase Order; unknown users are rejected outright
(`test_rbac_*`, `test_unknown_user_rejected`).

**The loop** — a stock read is auto with no proposal; an action stages a draft
without submitting (docstatus stays 0); approval commits, and the **read-back
verifies** the submitted record matches intent; reject writes nothing; a second
approval of the same proposal is refused (`test_read_is_auto` …
`test_double_approve_refused`).

**Blocked actions** — the GL delete is refused with a safe alternative offered
(`test_gl_delete_blocked`).

**Approve-with-edits + learning** — editing the rate before approving
recalculates the total, submits the *edited* values, passes read-back, and the
diff is stored as an org norm; clean vs edited approvals are tracked separately
on the skill, and `vetted` stays false until manually set
(`test_edit_then_approve_recalcs_and_learns`,
`test_clean_vs_edited_skill_tracking`).

**Memory** — explicit `remember` is used in the next draft; personal memory
never crosses users; **org-memory recall is permission-scoped** (an accounts
norm is invisible to a sales user); memories are visible and deletable only by
their owner; contradictions lower confidence; the 3rd identical correction
triggers the remember-suggestion (`test_explicit_remember_and_use` …
`test_repetition_triggers_suggestion`).

**Screen context** — "what's the status of this?" resolves against the document
the widget reports open; context reads still respect RBAC
(`test_context_aware_this`, `test_context_respects_rbac`).

**Daily brief** — permission-scoped per user (`test_brief_is_permission_scoped`).

**Audit** — the full chain (staged → approved → committed → memory_stored) is
recorded (`test_audit_full_chain`).

## End-to-end HTTP validation
Beyond unit tests, the full API was exercised through FastAPI's TestClient at
build time: users, brief, remember→use, draft→edit→approve→read-back, learned
notes returned, skills counters, RBAC refusal, screen context, memory
page + forget, both frontends served, audit tail. (Repeatable: see the smoke
commands in the build log / README.)

## Bugs found by this suite during the build (why testing is not theatre)
1. New `Skill` rows had `runs=None` before DB flush → `+= 1` crashed. Fixed by
   explicit initialization.
2. The `remember` command lowercased stored values ("Unit-2" → "unit-2"). Fixed
   by parsing the original-case text.
3. (Earlier build) in-memory SQLite didn't share across API worker threads.
   Fixed by file-backed DB default.

## Validation limits — read this honestly
- Validated against **FakeERP**, not a live ERPNext. The adapter contract is
  tested; the real Frappe integration is not (needs your staging site — doc 03
  includes the go-live checklist, which requires re-running this suite against
  the real adapter).
- Interpretation is the deterministic RuleInterpreter. The LLM interpreter will
  need its own evaluation set (intent-accuracy on real user phrasings) before
  production; the governance and RBAC layers are specifically designed so that
  interpreter mistakes can never bypass them.
- Single-process, SQLite, no auth on the HTTP layer: pilot-grade, not
  internet-facing. Production hardening map is in doc 02.

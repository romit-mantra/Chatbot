# 02 — Architecture (this codebase)

## Layout

```
backend/
  erp.py         ERP adapter: interface + FakeERP + FrappeAssistantAdapter stub
  governance.py  risk-tier gate + RBAC (roles, users, permission checks)
  store.py       SQLAlchemy models: Job, Proposal, AuditLog, Memory, Skill
  memory.py      MemoryService: scoped recall, learning hooks, skills
  engine.py      the core loop; RuleInterpreter / LLMInterpreter
  auth.py        login/sessions/tokens + act-as-user CredentialStore
  app.py         FastAPI endpoints (Bearer-token enforced); serves both frontends
  tests/         41-test validation suite (offline)
frontend/
  index.html     the portal
  widget.html    the embedded in-ERP widget (simulated Desk + context-aware chat)
docs/            this documentation set
```

## Request flow

```
channel (portal / widget)                     [surface only]
   │  POST /api/command {text, actor, context}
   ▼
Engine.handle_command
   ├─ user known?  ──no──► refused
   ├─ Interpreter.interpret(text, erp, context) ──► Intent
   ├─ MemoryService.recall(actor)   [permission-scoped]
   ├─ RBAC guard: user_can(actor, doctype, action)  ──deny──► audited refusal
   ├─ reads (auto tier) ──► reply
   └─ actions:
        gather ─► create_draft (auto tier, reversible)
        classify(doctype, "submit", value) ─► APPROVE | BLOCK
        APPROVE ─► Proposal row (pending) ─► reply + approval card
   ▼
POST /api/proposals/{id}/approve {approver, edits?}
   ├─ RBAC: approver must hold submit on the doctype
   ├─ edits? ─► update_draft + recalc dependents + edited=True
   ├─ erp.submit ─► read-back verify ─► executed | verify_failed
   ├─ MemoryService.on_approved  (edit diff → org norm; clean → reinforce)
   └─ MemoryService.skill_run    (runs/clean counters; vetting manual)
```

Every arrow that matters writes to `AuditLog`.

## Design invariants (do not break these when extending)

1. **The engine talks only to `ERPAdapter`.** No direct Frappe/HTTP calls anywhere
   else. This is what makes FakeERP ↔ real ERPNext a config change.
2. **Governance lives in `governance.classify` and the RBAC map — nowhere else.**
   No tool or endpoint may submit a high-tier write without passing through
   `Engine.approve`. The model can propose; only the approve path commits.
3. **Memory recall goes through `MemoryService.recall`,** which applies the
   permission filter. Never query the Memory table directly for recall.
4. **Interpretation is pluggable.** Anything the interpreter returns is still
   subject to RBAC + the gate; a jailbroken prompt cannot bypass either.
5. **Identity comes from the session token, never the request body.** All
   endpoints resolve the user via `auth.resolve`; act-as-user binds the ERP
   adapter per user through `CredentialStore` (`auth.py`).
6. **Channels are thin.** A new channel (WhatsApp, voice) is a new translator to
   `POST /api/command` — never new business logic.

## Production hardening map (what changes at scale)

| This build | Production |
|---|---|
| FakeERP | FrappeAssistantAdapter → Frappe Assistant Core on your ERPNext (doc 03) |
| RuleInterpreter | LLMInterpreter (local-model-first router + frontier fallback) |
| RBAC map in governance.py | ERPNext's own permission engine via per-user OAuth (act-as-user) |
| SQLite | PostgreSQL (same models; change `CHITRAGUPTA_DB`) |
| In-process engine | workers behind a queue; durability via a workflow engine (Temporal) |
| Demo passwords + local sessions | SSO / Frappe OAuth binding via `CredentialStore.adapter_factory` |
| Approval in-app only | + ERPNext Workflow chain integration, WhatsApp approvals |
| Correction counter in memory | + vector store for episodic memory / knowledge index |

The blueprint (chitragupta-production-blueprint.md) remains the authority on the
full target architecture and slice sequencing; this codebase is Slices 1–2 plus
the memory layer and the embedded channel pulled forward.

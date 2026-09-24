# 01 — Chitragupta: Product Overview & Features

## What Chitragupta is

A single assistant the whole company talks to in plain language — from the ERPNext screen itself, a portal, a phone, or WhatsApp — that can fetch anything and do anything in the ERP the person is allowed to, prepares every change for a human to confirm, keeps a complete record of it all, and learns from every interaction.

It is the governed, conversational front door to the ERP for people who are not ERP experts. It is general by design: there is no fixed catalogue of use cases — there is the ERP, and Chitragupta can operate any part of it the asker is permitted to.

## The core loop

Every request runs the same six steps:

1. **Interpret** — understand the request in the user's words, enriched by memory and (in the embedded widget) the document on screen.
2. **Gather** — permission-checked reads from the ERP plus recall from memory.
3. **Draft** — any change is prepared as a validated, reversible draft. Never a direct write.
4. **Confirm** — a plain-language approval card; the human can edit any field before approving.
5. **Commit** — submit through the ERP's validated API, then read the record back to verify it matches intent.
6. **Learn** — approvals reinforce; edits teach; skills accumulate.

Fetches stop after step 2 (reads need no approval). Actions run the whole loop.

## Feature reference (everything in this build)

### Conversation & channels
- **Portal** (`/`) — chat, role-aware quick actions, daily brief, memory page, skills view, live activity stream.
- **Embedded in-ERP widget** (`/widget`) — Chitragupta inside the ERPNext Desk. Inherits the user's login (no second sign-in) and sees **which document is open**, so "what's the status of this?" needs no explanation. (In this build the Desk is simulated; in production the widget ships as a Frappe custom app.)
- Both channels drive the **same backend and the same governance** — a channel is a surface, never a bypass.

### Identity, roles & permissions (the individual assistant)
- Every request carries a user. Each user has roles; each role has doctype-level permissions (read / write / submit).
- **RBAC is enforced in the engine before any ERP call** — a Sales user cannot draft a Purchase Order, and cannot approve one either, no matter what the model says. In production this backstop is ERPNext's own permission engine via act-as-user OAuth.
- The result is one assistant that becomes an *individual* assistant per user: same brain, each person's identity, rights, memory, and brief.

### Governance (drafts-first, engineered in)
- Risk tiers, decided in one auditable place: **auto** (reads, analytics, draft creation), **needs approval** (any submit/update/cancel; all financial doctypes), **blocked** (GL deletes, closed-period edits, anything above the hard value cap — approval cannot override).
- **Approve-with-edits**: the approval card is editable; dependent fields (e.g. total = qty × rate) recalculate; the human's diff is applied to the draft before submit.
- **Read-back verification** after every commit.
- Idempotent submit; double-approval refused; blocked proposals cannot be approved.

### Memory & learning (three memories, three rules)
- **Personal memory** (per user): explicit — "remember default_warehouse Unit-2" — and suggested after repeated identical corrections (confirm-before-store; nothing silent). Used automatically (e.g. the warehouse lands in the next draft).
- **Organizational memory**: learned from outcomes. A clean approval reinforces the norms in the draft; a human **edit before approval stores the corrected value as the new norm**. Contradictions lower confidence and surface as questions, not silent picks.
- **Skill memory**: each approved run of a task increments run/clean counters; the clean-approval rate is the "is it learning?" metric; **vetting stays manual** — a skill never graduates itself.
- Rule 1: **recall is permission-scoped** — personal memory never crosses users; org memory only surfaces to users who can read the doctype it was learned about. Memory can never become a permission bypass.
- Rule 2: **visible & correctable** — every user has a "what you've taught me" page; every memory is deletable, only by its owner.
- Rule 3: **conflicts resolve by asking** — recency wins only after the human confirms.

### Assistant behaviors
- **Daily brief** per user, permission-scoped: pending approvals *they* can act on, low-stock alerts, open POs. The morning "here's your day".
- Human-language refusals with a path forward ("your role doesn't allow this — I can hand it to someone who can").
- Safe alternatives on blocked actions (a reversing Journal Entry instead of a GL delete).

### Audit
- Every stage, approver, edit, memory event, RBAC denial, and commit is written to the audit table with actor and timestamp. The name is the promise: the complete ledger.

## What is deliberately stubbed in this build
- **The ERP** is `FakeERP`, a faithful in-memory stand-in with Frappe semantics. The real path is `FrappeAssistantAdapter` (six methods to implement against Frappe Assistant Core) — see doc 03. Swapping is a config change; the engine is adapter-agnostic.
- **Interpretation** now ships both brains: `RuleInterpreter` (deterministic; used when no key is set and as the always-on fallback) and the real `LLMInterpreter` (`interpreter_llm.py`) — set `ANTHROPIC_API_KEY` and the chat understands free-form language. Model output is whitelist-validated and everything still passes RBAC + the gate; tests prove a jailbroken model cannot bypass either.
- WhatsApp/voice channels, ERPNext Workflow-chain integration, automode schedules, and the vector-store knowledge index are later slices per the blueprint.

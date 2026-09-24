# 05 — Memory & Learning Specification

Chitragupta remembers three different things, with different owners and rules.

## The three memories

**Personal memory** — owned by each user. Preferences: default warehouse, units,
CC habits. Stored two ways: *explicitly* (`remember default_warehouse Unit-2` in
chat, or `POST /api/memory`) and *by suggestion* — the repetition detector
(`MemoryService.track_correction`) counts identical corrections and, on the 3rd,
offers to remember (confirm-before-store; the system never silently learns a
preference). Personal memory is used automatically (e.g. the warehouse appears
in the next PO draft) and **never crosses users**.

**Organizational memory** — owned by the company. Norms learned from outcomes:
- A **clean approval** reinforces the facts embedded in the draft (e.g. the
  item↔supplier rate norm gains confidence).
- A **human edit before approval** is the highest-value signal: the diff between
  proposed and approved is stored as the new norm
  (`MemoryService.on_approved`). The user was going to correct the draft anyway
  — the correction costs them nothing and teaches the system.
- A **contradiction** (new value ≠ stored value) replaces the value but drops
  confidence to 0.6, so the assistant treats it as "suggest, don't assume" and
  asks rather than silently picking. Reinforcement raises confidence back
  (+0.1 per confirmation, capped at 1.0).

**Skill memory** — owned by the company, vetted like code. Every approved run of
a task type increments `runs`; approvals without edits increment
`approved_clean`. The **clean rate** (`approved_clean / runs`) is the visible
"is it learning?" metric — the number to chart week over week. `vetted` is a
manual flag: a skill never graduates itself to unsupervised use; a human
promotes it after the clean rate earns trust. (Automode consuming vetted skills
is a later slice.)

## The three rules (non-negotiable)

1. **Recall is permission-scoped.** `MemoryService.recall(user)` returns only
   (a) that user's personal memory and (b) org memories whose scope-doctype the
   user can *read* under RBAC. This closes the subtle hole where something
   learned while helping the accounts head leaks to a sales clerk. Memory must
   never become a permission bypass — the test
   `test_org_memory_recall_is_permission_scoped` guards this forever.
2. **Memory is visible and correctable.** Every user has a "what you've taught
   me" page (portal, left rail; `GET /api/memory/{user}`), and every personal
   memory is deletable — by its owner only (`test_memory_visible_and_deletable_
   only_by_owner`). When a memory is used, the assistant says so in the draft.
3. **Conflicts resolve by asking.** Recency wins only after the human confirms;
   the confidence drop plus the suggestion pattern implements this.

## Data model

`Memory(kind, owner, scope_doctype, key, value, confidence, source, updated_at)`
- `kind=personal`: `owner` set, `scope_doctype` empty.
- `kind=org`: `scope_doctype` set (the permission scope), `owner` empty.
- `source`: `explicit` | `learned`.

`Skill(name, doctype, runs, approved_clean, vetted, updated_at)`

All memory writes and deletions are audited (`memory_stored`,
`memory_forgotten`).

## Production extensions (per the blueprint)
- A vector index over org memory + episodic task history for semantic recall
  (the structured store stays the source of truth; the index is for retrieval).
- Time-decay on confidence for norms not reinforced in N months.
- Approved-trajectory export as the dataset for optional on-prem fine-tuning.

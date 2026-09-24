# 03 — Connecting Chitragupta to your ERPNext

This build runs against `FakeERP`, a faithful stand-in. Connecting the real ERP
is deliberately a **narrow, well-defined job**: implement six methods in one
class. Nothing in the engine, governance, memory, or frontends changes.

## Prerequisites
1. An ERPNext instance you control (self-hosted or Frappe Cloud). **Start with a
   test/staging site, never production.**
2. [Frappe Assistant Core](https://github.com/) (the open-source MCP server for
   ERPNext) installed on that site:
   ```bash
   bench get-app frappe_assistant_core <repo-url>
   bench --site yoursite.local install-app frappe_assistant_core
   ```
   It exposes ERPNext's documents, reports, and metadata as MCP tools, with the
   calling user's permissions and ERPNext's own audit applied.
3. Credentials:
   - **Pilot (single service user):** an API key/secret pair for one dedicated
     ERPNext user with the roles you want the assistant to have. Fastest start;
     every action is attributed to the bot user.
   - **Production (act-as-user):** per-user OAuth tokens so Chitragupta acts as
     each real employee. This is what makes ERPNext's permission engine the hard
     backstop and keeps audit attribution per-person. Required before real use.

## The six methods

In `backend/erp.py`, implement `FrappeAssistantAdapter`:

| Method | Frappe operation |
|---|---|
| `describe(doctype)` | fetch DocType metadata (fields, links, mandatory rules) |
| `search(doctype, filters)` | list documents with filters |
| `get(doctype, name)` | fetch one document |
| `create_draft(doctype, doc)` | insert a document (stays at docstatus 0) |
| `update_draft(doctype, name, patch)` | update an unsubmitted document |
| `submit(doctype, name)` | submit the document (docstatus 0 → 1) |

Each maps to a tool exposed by Frappe Assistant Core (document create/read/
update/submit, list, metadata). Keep these semantics exactly:
- `create_draft` must NOT submit. Frappe inserts at docstatus 0 by default —
  that is the drafts-first guarantee.
- `submit` must be idempotent from Chitragupta's side (re-submitting an already
  submitted doc returns it unchanged rather than erroring).
- `update_draft` must refuse on a submitted document.
- All methods raise `ERPError` on failure; the engine handles it.

## Switch the adapter

In `backend/app.py`:
```python
# erp = FakeERP()
from erp import FrappeAssistantAdapter
erp = FrappeAssistantAdapter(
    base_url=os.environ["ERP_URL"],          # https://your-site
    token=os.environ["ERP_TOKEN"],           # per-user OAuth (prod) or api key:secret (pilot)
)
```
Set the environment and restart. That is the entire switch.

## Map users

In this build, users/roles live in `governance.py` (`USERS`, `ROLE_PERMS`) as a
stand-in for ERPNext's permission engine. When you move to act-as-user OAuth:
- keep the in-engine RBAC as a *second* layer (defense in depth), synced from
  ERPNext roles, or
- relax it to pass-through and let ERPNext be the sole enforcer.
Either way the engine's rule holds: no ERP call without a permission check, and
no high-tier write except through the approve path.

## Wire the approval chain (recommended next)

ERPNext's native **Workflow** engine holds your real multi-level approval
hierarchy. Integrate by treating Chitragupta's proposal approval as the *same*
approval: on approve, apply the workflow action (Frappe Assistant Core /
Composio-style `apply workflow` tools exist) instead of a bare submit, so the
document advances through the company's actual chain and the right next
approver is notified. Never build a parallel chain.

## Validation checklist before real users

- [ ] All 23 tests pass against the real adapter pointed at the STAGING site
      (run the suite with the adapter env vars set).
- [ ] Draft-then-approve round trip on one real DocType; confirm docstatus 0 →
      approval → docstatus 1, and read-back verified.
- [ ] RBAC: a low-permission test user is refused draft and approve.
- [ ] Blocked action (GL Entry delete) refused end to end.
- [ ] Audit rows visible for the full chain.
- [ ] Kill access quickly: know how to revoke the token / disable the bot user.

## Embedding the widget in the real Desk

`frontend/widget.html` simulates the Desk. In production, ship the widget as a
small **Frappe custom app**: a JS bundle injected into the Desk that (a) reads
the logged-in session for identity, (b) reads the current route for screen
context (`frappe.get_route()` gives doctype + name), and (c) posts to
`/api/command` with `{actor, context}`. The backend is unchanged — the widget
is just another thin channel.

---

## ✅ Going live — the actual runbook (Frappe native REST)

The adapter is **built**: `backend/erp_frappe.py` (`FrappeERP`). It uses Frappe's
native REST API, so **no extra app install is required**.

### 1. Create a limited bot user (never Administrator)
In ERPNext: Users → Add User → e.g. `chitragupta-bot@yourcompany.com`,
Role Profile: **Purchase** (start narrow). Save, then open the user →
Settings → API Access → **Generate Keys**.

Using Administrator would give the agent god-mode over payroll and the ledger
and defeat the entire safety model. The connection check warns you if you do.

### 2. Set the environment (credentials never live in code)
```bash
export ERP_URL="http://<your-erp-host>:8000"
export ERP_API_KEY="<bot key>"
export ERP_API_SECRET="<bot secret>"
```

### 3. Prove the connection (READ-ONLY — writes nothing)
```bash
cd backend && python check_connection.py
```
Confirms auth, DocType introspection, reads on Item/Supplier/Purchase Order,
and that the bot **cannot** read a sensitive doctype (Salary Slip) — proof the
limited role is doing its job.

### 4. Run the live loop (drafts first; prompts before any submit)
```bash
python live_smoke.py --no-submit   # safest: drafts only, submits nothing
python live_smoke.py               # drafts, then asks you to confirm the submit
```
Proves on real data: read → draft (docstatus 0) → governance gate → human
confirm → submit (docstatus 1) → read-back verification → audit trail.

### 5. Run the whole app against the live ERP
```bash
uvicorn app:app --reload     # ERP_URL present -> FrappeERP; absent -> FakeERP
```
That one env var is the entire switch. On boot it prints which ERP it bound to.

### Prerequisites in your ERPNext
At least one **Item**, one **Supplier**, and one **Company** must exist (the PO
workflow needs them). A fresh site's onboarding wizard creates these.

### If something fails
- `401/403` → wrong keys, or the bot's role lacks permission on that DocType.
- `cannot reach ERP` → host/port wrong, or the backend can't route to the VM.
- `no Item/Supplier records` → create one in ERPNext first.
- Submit rejected by ERPNext → a mandatory field is missing for your site's
  config; `describe("Purchase Order")` lists required fields.

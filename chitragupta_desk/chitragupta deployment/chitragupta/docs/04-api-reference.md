# 04 — API Reference

Base URL: wherever the backend runs (default `http://127.0.0.1:8000`).
All bodies are JSON.

## Authentication (required on everything below except login)
`POST /api/login` `{"user":"ravi","password":"demo"}` →
`{"token","user","full_name","roles"}` (demo password is `demo` for all users;
replace with SSO / Frappe OAuth per docs/03).
Send `Authorization: Bearer <token>` on every other request. **Identity is
resolved from the token only — request bodies cannot carry or spoof an
actor/approver.** Sessions last 12h; `POST /api/logout` invalidates.
401 responses mean missing/invalid/expired token. Logins and failures are
audited (`login`, `login_failed`).

## Users
`GET /api/users` → `{"users":[{"id","full_name","roles":[...]}]}`

## Command (the core loop)
`POST /api/command`
```json
{"text": "raise a PO for the screw",
 "context": {"doctype": "Purchase Order", "name": "PUR-2026-00031"}}
```
`context` is optional — the embedded widget sends the document on screen so
"this/it" resolves. Response:
```json
{"job_id": 1,
 "reply": "Drafted a PO to **ACME Fasteners** ...",
 "activity": [{"tag":"plan|info|go|hold|stop","text":"..."}],
 "proposals": [{"id":1,"summary":"...","tier":"approve","reason":"...",
                "doctype":"Purchase Order","value":74400.0,"actor":"ravi",
                "fields":{...}}]}
```
Reads return with `proposals: []`. RBAC refusals return a normal reply plus a
`stop` activity line (and an audit row).

## Proposals
`GET /api/proposals` → pending proposals visible to the session user
(their own, plus any they hold submit permission for).

`POST /api/proposals/{id}/approve`
```json
{"edits": {"rate": 12.10}}
```
The approver is the session user.
`edits` is optional (approve-with-edits). Dependent fields recalculate
(total = qty × rate). Response:
```json
{"proposal_id":1, "submitted":"PUR-2026-00040", "readback_ok":true,
 "status":"executed", "learned":["learned: Purchase Order.rate -> 12.1 ..."]}
```
Errors: unknown proposal, not pending, blocked tier, or approver lacks submit
permission on the doctype.

`POST /api/proposals/{id}/reject` `{}` → `{"status":"rejected"}`

## Daily brief
`GET /api/brief` (session user) →
`{"greeting":"Morning, Ravi (Purchase).","items":[...],"skills":[...],"pending":[...]}`
Items are permission-scoped: users only see what their roles can read/act on.

## Memory
`GET /api/memory` → the session user's personal memories (visible page).
`POST /api/memory` `{"key","value"}` → store explicitly.
`DELETE /api/memory/{id}` → forget (owner = session user; returns `{"deleted":bool}`).
In chat, `remember <key> <value>` does the same via the command endpoint.

## Skills
`GET /api/skills` → `[{"name","doctype","runs","approved_clean","clean_rate","vetted"}]`
`clean_rate` (approved-without-edits) is the learning metric; `vetted` is manual.

## Audit
`GET /api/audit` → last 200 events, newest first:
`{"ts","event","actor","detail"}` — events include `proposal_staged`, `approved`,
`committed`, `rejected`, `blocked`, `rbac_denied`, `memory_stored`,
`memory_forgotten`.

## Frontends
`GET /` → the portal. `GET /widget` → the embedded in-ERP widget.

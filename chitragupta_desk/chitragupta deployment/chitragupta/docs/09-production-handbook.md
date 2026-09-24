# 09 — Production Handbook

The complete operational reference: deployment to a real ERP, costs, memory
model, limitations, and what to watch. Read this before rollout.

## 1. What ships in this package

```
chitragupta/
├── backend/                 FastAPI backend (18 modules, 131 tests)
│   ├── app.py               API + CORS + /api/version build stamp
│   ├── engine.py            Six-step governed loop + agent + learning
│   ├── planner.py           LLM planning + analytics + fuzzy/date filters
│   ├── agent.py             Multi-step tool loop (compose anything)
│   ├── governance.py        Risk tiers, write-gate RBAC, memory scope
│   ├── auth.py              Sessions, SSO verify, CredentialStore (act-as-user)
│   ├── memory.py            Personal/org/skills + learning loop + stats
│   ├── erp.py / erp_frappe.py  Adapter interface / live ERPNext REST
│   ├── dates.py files.py reports.py   Normalisation, uploads, report files
│   ├── store.py             SQLAlchemy schema (jobs/proposals/audit/memory/skills)
│   ├── check_connection.py  diagnose_po.py   Live-ERP diagnostics
│   └── tests/               131 tests, 16 suites
├── erpnext_embed/chitragupta_desk.js   The Desk widget
├── frappe_app/              INSTALL.md + chitragupta_desk_api.py (SSO method)
├── frontend/                Standalone portal (optional)
├── docs/ 01–09              This documentation set
└── requirements.txt
```

## 2. Production deployment (real company ERP)

### 2.1 Prerequisites
- ERPNext v14/v15 site, bench access
- A Linux host (or the ERP server itself) for the backend — Windows works but
  Linux + systemd is recommended for production
- An Anthropic API key with billing enabled
- HTTPS in front of both ERP and backend (reverse proxy, e.g. nginx)

### 2.2 The bot user (do this first, and keep it narrow)
1. Create `chitragupta-bot@<company>.com`, role **Purchase User** only (add
   roles per module as you expand — never Administrator in production).
2. User → API Access → Generate Keys → note key/secret.
3. Verify: `python check_connection.py` prints the bot identity and a
   permission matrix. 403s on payroll/GL are CORRECT — that is the fence.

### 2.3 Backend as a service
```bash
python -m venv /opt/chitragupta/venv
/opt/chitragupta/venv/bin/pip install -r requirements.txt
```
`/etc/chitragupta.env` (chmod 600, owned by the service user):
```
ERP_URL=https://erp.company.com
ERP_API_KEY=...
ERP_API_SECRET=...
ANTHROPIC_API_KEY=...
CHITRAGUPTA_SSO_SECRET=<openssl rand -hex 32>
CHITRAGUPTA_DB=postgresql+psycopg2://chitragupta:...@localhost/chitragupta
```
`/etc/systemd/system/chitragupta.service`:
```
[Unit]
Description=Chitragupta backend
After=network.target
[Service]
EnvironmentFile=/etc/chitragupta.env
WorkingDirectory=/opt/chitragupta/backend
ExecStart=/opt/chitragupta/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8001
Restart=always
User=chitragupta
[Install]
WantedBy=multi-user.target
```
Put nginx in front with TLS; allow the ERP origin. Verify `/api/version`.

### 2.4 The Desk widget (Frappe app)
Follow `frappe_app/INSTALL.md`:
`bench new-app chitragupta_desk` → copy widget JS (line 1 =
`window.CHITRAGUPTA_URL = "https://chitragupta.company.com";`) → copy
`chitragupta_desk_api.py` as `api.py` → `app_include_js` in hooks →
install-app → build → clear-cache. **Bench must be running during build**
(redis warnings = the asset did not register).

### 2.5 Act-as-user (SSO)
```bash
bench --site <site> set-config chitragupta_sso_secret "<same 64-char secret>"
bench restart
```
Verify: `/api/method/chitragupta_desk.api.get_session_assertion` returns
signed JSON; widget header shows the real name + roles; backend log shows
`POST /api/sso 200`.
**Clocks must agree.** The replay window is 900 s (`CHITRAGUPTA_SSO_MAX_AGE`).
Run NTP on both hosts; on VMs disable the hypervisor's competing time sync.

### 2.6 Go-live checklist
- [ ] `/api/version` shows the deployed build; 131 tests pass on the server
- [ ] check_connection.py: bot fenced correctly (403 on payroll)
- [ ] SSO: real user + roles in header; forged assertion rejected (401)
- [ ] Draft→approve→read-back on a test PO; owner is the real user
- [ ] HTTPS everywhere; secrets only in the env file; keys rotated if they
      ever appeared in chats/screenshots (they did during development)
- [ ] Postgres in use; nightly DB backup scheduled
- [ ] /api/learning returns; note the starting clean_rate

## 3. Costs (estimates — verify current pricing at anthropic.com/pricing)

The only per-use cost is the Anthropic API (model: Claude Sonnet). Rough
planning numbers at typical Sonnet pricing (~$3 / 1M input tokens, ~$15 / 1M
output tokens):

| Interaction | Tokens (typ.) | Est. cost |
|---|---|---|
| How-to / explain | 1–2k in, 300 out | < $0.01 |
| Context Q&A on a document | 3–5k in, 300 out | ~$0.01–0.02 |
| Analytics over ≤200 rows | 5–15k in, 500 out | ~$0.02–0.06 |
| Document creation (multi-turn) | 2–4 calls | ~$0.02–0.05 |
| Agent request (≤8 steps) | 8–20k total | ~$0.05–0.25 |

Pilot planning: 20 users × 15 messages/day ≈ 300 msgs/day ≈ **$5–15/day**
(₹400–1,250), i.e. roughly **$150–450/month** — dominated by analytics and
agent usage. Levers if needed: `CHITRAGUPTA_MODEL=claude-haiku-4-5-...` for
planning (≈5–10× cheaper), caching how-to answers, lowering the row cap.
Infrastructure cost is your own hardware; no other SaaS fees.

## 4. Memory model — what persists, what doesn't

| Layer | Where | Survives restart? | Survives refresh? |
|---|---|---|---|
| Personal preferences ("remember X") | DB | ✅ | ✅ |
| Org norms (learned from edits) | DB | ✅ | ✅ |
| Skills counters / audit / proposals | DB | ✅ | ✅ |
| Conversation thread (12 turns/user) | backend RAM | ❌ | ✅ (backend unaffected) |
| Widget chat display | sessionStorage | n/a | ✅ same tab; ❌ new tab/browser restart |
| SSO session bindings (act-as-user) | backend RAM | ❌ (users reload page to re-bind) |
| Backend login tokens | DB (12 h TTL) | ✅ until expiry | ✅ |

So: **learned knowledge is permanent; conversational context is ephemeral.**
A backend restart clears live threads and session bindings (harmless — users
reload); it never loses learning, audit, or pending approvals.

## 5. Known limitations (honest)

- **Row cap**: analytics feeds ≤200 rows (≈60 if very wide) to the model.
  Fine at pilot scale; very large tables need server-side aggregation (roadmap).
- **Attachments**: first 3 files, ~6k chars each; scanned/image-only PDFs and
  images are not read (no OCR/vision yet).
- **No streaming**: answers arrive whole; complex analytics can take 3–10 s
  (the thinking indicator covers this).
- **Language**: English only for now (multilingual is on the roadmap).
- **Approvals** use Chitragupta's own gate; native ERPNext Workflow chains are
  not yet driven (roadmap item 1).
- **Model knowledge** is general ERPNext knowledge; company-specific processes
  are only known if learned or asked.
- **The model can be wrong.** Analytics are computed from real rows but the
  final arithmetic/wording is model-generated — the governance design assumes
  human review for anything that matters, and no write ever bypasses approval.
- **Concurrency**: single uvicorn process is right for a 20-user pilot; scale
  out behind nginx with multiple workers when usage grows (session bindings
  are per-process — move CredentialStore to the DB/redis at that point).
- **"Bug-free"** is a claim no honest engineer makes. What is true: 131
  automated tests pass, every field bug found during live testing has a
  regression test, and the system fails safe (errors surface honestly; writes
  always require approval).

## 6. Security model summary

- Widget inherits ERP login; identity proven by ERP-signed HMAC assertion
  (replay-bounded); session binding verified against the ERP before use.
- Reads: ERPNext's permission engine is the authority. Writes: gated in-engine
  by role AND by ERPNext. Ledger deletes/cancels: blocked outright.
- Every write is a proposal a human approves; read-back verifies commits;
  everything is audited (who, what, when, edited or not).
- Uploaded files and attachments are context, never commands (tested).
- Rotate any credential that ever appeared in a screenshot or chat.

## 7. Support & diagnostics

- `/api/version` — what build is running (check FIRST when anything looks off)
- `/api/learning` — learning scorecard (chart clean_rate weekly)
- `check_connection.py` / `diagnose_po.py` — live-ERP ground truth
- Backend log — every request; SSO failures state their precise reason
- The audit table — the full history of proposals, approvals, learning events

## 8. Pending roadmap (priority order)

1. **ERPNext Workflow chains** — route approvals through the company's real
   multi-level chains (right approver by amount/role).
2. **Server-side aggregation** — remove the row cap for company-scale tables.
3. **Streaming responses** (SSE) — perceived speed.
4. **WhatsApp channel** — approvals-on-phone first, then full chat + voice.
5. **Multilingual** — Hindi/Gujarati for the shop floor.
6. **Automode + vetted skills** — high-clean-rate tasks graduate to
   supervised autonomy with a kill switch.
7. **OCR/vision** for scanned attachments.
8. **Persistent session bindings + horizontal scaling** (DB-backed
   CredentialStore) when moving past one process.

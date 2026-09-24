# Chitragupta — The governed AI assistant inside your ERP

One assistant the whole company talks to in plain language — embedded in
ERPNext — that fetches anything and does anything the logged-in person is
allowed to, drafts every change for human approval, audits everything, and
measurably learns from every correction.

## Quick start
1. **Read `docs/09-production-handbook.md`** (or the PDF set in `pdfs/`).
2. Backend: `pip install -r requirements.txt`, set the five env vars
   (ERP_URL, ERP_API_KEY, ERP_API_SECRET, ANTHROPIC_API_KEY,
   CHITRAGUPTA_SSO_SECRET), run `uvicorn app:app --host 0.0.0.0 --port 8001`.
   Boot log must show "LIVE ERPNext" and "language brain: LLM".
3. Widget + SSO on the ERP: follow `frappe_app/INSTALL.md`.
4. Verify: `/api/version`, then `python backend/check_connection.py`.

## Documentation map
- `docs/01–08` — vision, architecture, ERP connection, API, memory, testing,
  embedding, current status
- `docs/09-production-handbook.md` — deployment, costs, memory model,
  limitations, security, roadmap (START HERE for go-live)
- `pdfs/` — the presentable document set (Overview, Deployment Guide,
  Technical Reference, Operations Handbook)

## State of the build
- 131 automated tests across 16 suites, all passing (run:
  `cd backend && python -m pytest tests/`)
- Verified against live ERPNext: governed PO creation with read-back,
  act-as-user SSO end-to-end, analytics on real data, attachments, reports
- Every bug found in live testing has a regression test

## The honest one-liner on quality
"Bug-free" is not a claim honest engineers make. What is true: the system is
extensively tested, fails safe (errors surface honestly; no write ever
bypasses human approval), and tells you what build it's running
(`/api/version`) and whether it's learning (`/api/learning`).

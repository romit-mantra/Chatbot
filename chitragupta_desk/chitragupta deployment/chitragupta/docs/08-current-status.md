# 08 — Current Status (post live-deployment)

*Updated after the live ERPNext integration and field-testing phase.*

## Journey summary

Chitragupta went from architecture to a **working assistant embedded inside a
live ERPNext** in the following arc:

1. **Blueprint & competitive scan** — north-star definition, governance model,
   build-vs-adopt decisions; scan of changAI, ChatNext, Next AI, Frappe
   Assistant Core, Definable AI. Conclusion: the embedded, screen-aware,
   *governed-action* assistant is unoccupied ground.
2. **Product core** — the six-step loop (interpret → gather → draft → confirm →
   commit → read-back → learn), governance tiers, RBAC, three-part memory,
   auth/session layer, portal + widget.
3. **Live integration** — connected to an on-prem ERPNext VM (Frappe REST,
   token auth, limited `chitragupta-bot` user). First real PO drafted and
   submitted through the full governed loop with read-back verification.
4. **Embedded channel** — Frappe custom app (`chitragupta_desk`) loads the
   chatbox on every Desk page; inherits the ERP login; reads
   `frappe.get_route()` for screen context.
5. **Field-hardening** — every bug found by real usage fixed and
   regression-tested (see doc 06 and the list below).

## What works today (verified on the live ERP)

- **Ask anything on live data**: counts, sums, comparisons, supplier-wise
  breakdowns; fuzzy name matching ("zukerman" → "Zuckerman Security Ltd.").
- **Screen-context Q&A**: "who raised this PO?", "what's the amount of this?"
  answered from the real open document (owner, grand_total, ...).
- **How-to / knowledge**: "how do I raise a PO?" answered with menu paths and
  the user's role taken into account.
- **Governed document creation**: multi-turn gathering (details accumulate
  across messages), partial-name resolution (supplier/company/item), date
  normalisation (DD-MM-YYYY → ISO, run-together dates split), required-field
  completion (per-row schedule_date), then a draft + approval card. Nothing
  submits without a human.
- **Approve-with-edits** + read-back verification + full audit trail.
- **Comments**: "comment on PUR-ORD-2026-00012 and tag admin" — posts to the
  document timeline with @mention.
- **Downloadable reports**: "purchase order report in excel" → real
  .xlsx/.csv/.pdf with a Download button in the chat.
- **File upload**: CSV/Excel/PDF attached in chat becomes context (e.g. draft a
  PO from a quotation). Files cannot bypass the approval gate (tested).
- **Voice input**: browser dictation (Web Speech API), auto-send.
- **Memory**: personal preferences ("remember default_warehouse Unit-2") used
  in later drafts; org norms learned from human edits; permission-scoped
  recall; visible & deletable.
- **Self-diagnosis**: `/api/version` proves which build the server runs;
  `diagnose_po.py` and `check_connection.py` test the live ERP directly.

## Hard lessons now encoded in the product

| Lesson | Encoding |
|---|---|
| Frappe 404s on POST can be *validation* errors | `_call` surfaces `_server_messages`; never discards detail |
| DocType path must be %20-quoted (never `+`) | `quote()` everywhere; regression-tested |
| Item rows each need schedule_date | `_complete_doc` guarantees it; tested |
| Users type DD-MM-YYYY | `dates.py` normalises all date fields |
| Don't duplicate ERPNext's permission engine | Reads delegated to ERPNext; writes gated in-engine; memory has its own strict scope |
| Narrow bot role > convenient wide role | 403s treated as safety working; never widened to fix errors |
| Conversation must accumulate | Per-user 12-turn history to the planner |
| Deploy drift causes ghost bugs | `/api/version` build stamp |

## Current limits (honest)

- **Single identity**: everyone acts through one bot user (Purchase role);
  widget authenticates as a demo backend user. Multi-department rollout is
  blocked on act-as-user.
- Approval is Chitragupta's own gate; ERPNext Workflow chains not yet driven.
- SQLite, HTTP, terminal-run uvicorn: pilot-grade infra.
- Fixed op set (query/create/comment/report/...); not yet a fully generic
  compose-anything engine.
- WhatsApp/mobile channel and multilingual output not yet built.

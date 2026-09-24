# 07 — Putting the chatbox INSIDE ERPNext

This is the piece that makes Chitragupta appear in the ERP after login, docked
on the page, knowing who you are and what document you're looking at.

The widget is `erpnext_embed/chitragupta_desk.js`. The backend serves it at
`/embed/chitragupta_desk.js` so ERPNext can just include it by URL.

## How it works

- **Identity comes free.** The widget reads `frappe.session.user` — the user is
  already logged into ERPNext, so there is no second sign-in.
- **Screen context comes free.** It reads `frappe.get_route()`, so when you're on
  a Purchase Order form it knows *which* PO you're looking at. Ask "what's the
  status of this?" and "this" resolves. No competitor does this.
- **Governance is unchanged.** The widget is a thin channel: it POSTs to
  `/api/command` exactly like the portal. Reads answer instantly; anything that
  changes data comes back as an approval card. The gate lives in the backend and
  cannot be bypassed by a channel.

## Option A — Quick install (5 minutes, no app build)

Good for the demo on your VM.

1. Start the backend with your ERP connected **and reachable from the browser**:
   ```powershell
   $env:ERP_URL = "http://192.168.179.128:8000"
   $env:ERP_API_KEY = "<bot key>"
   $env:ERP_API_SECRET = "<bot secret>"
   uvicorn app:app --host 0.0.0.0 --port 8001
   ```
   Note `--port 8001`: ERPNext already uses 8000. And `--host 0.0.0.0` so the
   ERP page (a different origin) can reach it.

2. In ERPNext: **Customize → Client Script → New**
   - DocType: leave blank / choose "Purchase Order" to scope it, or use a
     Website Script for site-wide.
   - Script:
     ```javascript
     window.CHITRAGUPTA_URL = "http://<your-pc-ip>:8001";
     $.getScript(window.CHITRAGUPTA_URL + "/embed/chitragupta_desk.js");
     ```
   Save, reload the Desk, and the चि button appears bottom-right.

   *Client Scripts are per-DocType; for a site-wide widget use Option B.*

## Option B — Proper install (a small Frappe custom app)

This is the production route: the widget loads on every Desk page.

```bash
bench new-app chitragupta_desk
bench --site <site> install-app chitragupta_desk
```

Copy the widget in:
```
apps/chitragupta_desk/chitragupta_desk/public/js/chitragupta_desk.js
```

In `apps/chitragupta_desk/chitragupta_desk/hooks.py`:
```python
app_include_js = ["/assets/chitragupta_desk/js/chitragupta_desk.js"]
```

Then:
```bash
bench build --app chitragupta_desk
bench --site <site> clear-cache
```

Set the backend URL once, e.g. in the same hooks file via a `boot` value, or by
adding a one-line Client Script that sets `window.CHITRAGUPTA_URL`.

## CORS

The widget runs on the ERPNext origin and calls the Chitragupta backend — a
different origin. The backend allows this automatically for whatever is in
`ERP_URL`. To add more origins:
```powershell
$env:CHITRAGUPTA_ORIGIN = "http://192.168.179.128:8000"
```

## Authentication (the honest bit)

Today the widget logs into the Chitragupta backend with a demo user
(`window.CHITRAGUPTA_USER`, default `ravi`). That is fine for a pilot on a VM.

**Production:** the ERP session must be exchanged for a backend token, so the
assistant acts as the *real* logged-in employee with their real ERPNext
permissions. Two supported routes:
- **Frappe OAuth2** — the widget requests a token for the current user; the
  backend stores it in `CredentialStore` (`auth.py`) and every ERP call is made
  as that employee. This is the `adapter_factory` socket already built.
- **Shared-secret handshake** — the Desk passes a signed assertion of
  `frappe.session.user`; the backend trusts it and issues a session.

Until that is wired, everyone acting through the widget shares the bot user's
ERP permissions. Keep the bot's role narrow (Purchase only) — which is exactly
why the connection check warns if you use Administrator.

## Troubleshooting

- **No चि button** → the script didn't load. Check the browser console; check
  `CHITRAGUPTA_URL` is reachable from the browser (not just from the server).
- **"Can't reach the backend"** → CORS. Confirm the backend printed your ERP
  origin in its allowed list, and that it's bound to `0.0.0.0`, not `127.0.0.1`.
- **Approvals fail with a permission error** → the backend demo user (`ravi`)
  lacks submit rights for that doctype, or the ERP bot user's role does. Both
  layers are intentional.

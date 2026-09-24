# Installing Chitragupta as a Frappe app (loads on EVERY Desk page)

Website Settings → HTML Header only injects into the **public website**, not the
`/app` Desk. The reliable, production-correct way to get the chatbox on every
Desk page is a tiny Frappe custom app. It takes about five minutes.

Run these **on the ERPNext VM** (192.168.179.128), in your bench folder
(usually `~/frappe-bench`).

---

## 1. Create the app

```bash
cd ~/frappe-bench
bench new-app chitragupta_desk
```

It will ask a few questions. Answers don't matter much:

```
App Title       : Chitragupta Desk
App Description : Chitragupta assistant widget
App Publisher   : Mantra
App Email       : you@mantratec.com
App License     : MIT
```

## 2. Add the widget file

```bash
mkdir -p apps/chitragupta_desk/chitragupta_desk/public/js
nano apps/chitragupta_desk/chitragupta_desk/public/js/chitragupta_desk.js
```

Paste the **entire contents** of `erpnext_embed/chitragupta_desk.js` from the
Chitragupta project. Save (Ctrl+O, Enter, Ctrl+X).

> Shortcut: the backend serves it, so you can fetch it directly on the VM:
> ```bash
> curl http://192.168.179.1:8001/embed/chitragupta_desk.js \
>   -o apps/chitragupta_desk/chitragupta_desk/public/js/chitragupta_desk.js
> ```

## 3. Point it at your backend

At the very TOP of that JS file, add one line (so it doesn't rely on a global
being set elsewhere):

```javascript
window.CHITRAGUPTA_URL = "http://192.168.179.1:8001";
```

## 4. Tell Frappe to load it on every Desk page

```bash
nano apps/chitragupta_desk/chitragupta_desk/hooks.py
```

Find the commented-out line `# app_include_js = ...` and add (uncommented):

```python
app_include_js = ["/assets/chitragupta_desk/js/chitragupta_desk.js"]
```

`app_include_js` = loaded on every **Desk** (`/app`) page. That is exactly what
we want. (`web_include_js` would be the public website — not what we want.)

## 5. Install and build

```bash
bench --site <your-site-name> install-app chitragupta_desk
bench build --app chitragupta_desk
bench --site <your-site-name> clear-cache
bench restart      # if running as a service; skip for `bench start` dev mode
```

Not sure of the site name?
```bash
ls sites/          # your site is the folder that isn't 'assets' or 'common_site_config.json'
```

## 6. Verify

Hard-refresh the Desk (Ctrl+Shift+R). The **चि** button should now appear on
**every** page — Home, any List, any Form, any DocType — and survive refreshes.

Check the asset actually built:
```bash
ls -l sites/assets/chitragupta_desk/js/chitragupta_desk.js
```

---

## Troubleshooting

**Button doesn't appear**
- Browser console (F12) → look for a 404 on `/assets/chitragupta_desk/js/...`
  → the build didn't run. Re-run `bench build --app chitragupta_desk`.
- Still 404 → `bench --site <site> clear-cache && bench restart`.

**Button appears but says "can't reach the backend"**
- The VM must reach your PC. Test from the VM:
  ```bash
  curl http://192.168.179.1:8001/embed/chitragupta_desk.js | head -5
  ```
- If that hangs → Windows Firewall. On your PC, in an **Administrator**
  PowerShell:
  ```powershell
  New-NetFirewallRule -DisplayName "Chitragupta 8001" -Direction Inbound `
    -LocalPort 8001 -Protocol TCP -Action Allow
  ```

**Note on the demo Client Script**
Delete the old "Abhishek" Client Script on Purchase Order once this works —
otherwise the widget loads twice on PO forms. (The widget guards against double
loading, but keep it clean.)

---

## Why this is the right route

- Loads on every Desk page, survives refresh and navigation.
- It is how Frappe apps are meant to extend the Desk — no hacks.
- Version-controlled, deployable to your real ERP later with the same steps.
- The widget itself is unchanged: it still inherits the ERP session for
  identity, reads `frappe.get_route()` for screen context, and talks to the
  Chitragupta backend, which enforces RBAC and the approval gate.

---

## Act-as-user SSO (Phase A) — make the assistant act as the REAL logged-in user

1. Copy `frappe_app/chitragupta_desk_api.py` into the app as:
   ```
   apps/chitragupta_desk/chitragupta_desk/api.py
   ```
2. Set a shared secret on the SITE (generate a long random string):
   ```bash
   bench --site <site> set-config chitragupta_sso_secret "<long-random-string>"
   bench restart   # or restart bench start
   ```
3. Set the SAME secret for the backend (PowerShell on the PC):
   ```powershell
   $env:CHITRAGUPTA_SSO_SECRET = "<the same long-random-string>"
   ```
   and restart uvicorn.
4. Re-pull/rebuild the widget (it now tries SSO first):
   the header will show "acting as <Real Name> · <their roles>" instead of the
   demo user. If SSO isn't configured, it falls back to the demo login.

What this gives you now:
- The backend session is bound to the REAL ERP user (ERP-signed, replay-bounded).
- Their REAL ERPNext roles are fetched and drive the write-gate (an Accounts
  Manager can't draft POs; a Purchase User can't submit payments).
- Audit rows carry the real person.

What Phase B adds later (per-user ERP credentials / OAuth): ERP-side actions
also EXECUTE as that user (today they execute via the bot user), so ERPNext's
own permission engine enforces per-person on every call and document
ownership shows the real employee.

---

## Act-as-user Phase B — ERP actions EXECUTE as the real person

Nothing extra to install. With SSO (Phase A) configured, the widget now also
passes the user's own ERP session (sid + CSRF token) to the backend. The
backend FIRST verifies with ERPNext that the session genuinely belongs to the
asserted user, then binds a per-user adapter.

From then on, for that user:
- reads and writes hit ERPNext AS THEM -> ERPNext's own permission engine
  enforces per-person on every call (no shared bot permissions)
- created documents show THEIR name as owner; comments are attributed to them
- the widget header drops the "· via bot" suffix (visible confirmation)

Fallbacks (automatic, never breaking):
- no sid / probe fails / mismatched session -> Phase A (bot execution) with
  audit entries (erp_session_mismatch / probe_failed)
- expired session mid-use -> the ERP returns 401/403, surfaced honestly; the
  user reloads the ERP page to re-bind

Security notes:
- a mismatched sid is NEVER bound (tested) — presenting someone else's cookie
  gets you Phase A at most
- the bot token is never sent on per-user calls (tested)
- bindings live in backend memory only; restart clears them

# chitragupta_desk/api.py  — put this file in the Frappe app:
#   apps/chitragupta_desk/chitragupta_desk/api.py
#
# Then add the shared secret to the SITE config (NOT in code):
#   bench --site <site> set-config chitragupta_sso_secret "<long-random-string>"
# and set the same value for the backend:
#   $env:CHITRAGUPTA_SSO_SECRET = "<the same long-random-string>"
#
# What this does (act-as-user Phase A):
#   The Desk widget calls this whitelisted method. Frappe runs it AS the
#   logged-in user (frappe.session.user), so the identity is authoritative —
#   not something the browser claims. It returns a short-lived HMAC-signed
#   assertion {user, ts, sig}; the widget exchanges it at the backend's
#   /api/sso for a Chitragupta session bound to the REAL employee.
#
# Guest sessions are refused. Replay is bounded by the timestamp (the backend
# rejects assertions older than 120 seconds).

import hashlib
import hmac
import time

import frappe


@frappe.whitelist()
def get_session_assertion():
    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw("Not logged in")
    secret = frappe.conf.get("chitragupta_sso_secret")
    if not secret:
        frappe.throw("chitragupta_sso_secret not configured on this site")
    ts = str(time.time())
    sig = hmac.new(secret.encode(), f"{user}|{ts}".encode(),
                   hashlib.sha256).hexdigest()
    # Phase B: include the session id + CSRF token SERVER-SIDE, so the widget
    # doesn't depend on reading cookies (which fails when sid is HttpOnly).
    out = {"user": user, "ts": ts, "sig": sig}
    try:
        out["sid"] = frappe.session.sid
        out["csrf"] = frappe.sessions.get_csrf_token()
    except Exception:
        pass
    return out

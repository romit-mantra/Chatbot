"""Chitragupta backend — FastAPI. API reference: docs/04-api-reference.md.

Security model (since the auth build):
- POST /api/login issues a session token; everything else requires
  Authorization: Bearer <token>.
- Identity is resolved from the token ONLY. Request bodies no longer carry
  actor/approver — a client cannot claim to be someone else.
- Each request's ERP adapter is bound per user via CredentialStore
  (act-as-user scaffolding; shared bot adapter in pilot mode).
"""

from __future__ import annotations

import json
import os
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from erp import FakeERP
from store import make_session, AuditLog, audit as audit_log
from engine import Engine
from governance import USERS
from interpreter_llm import make_interpreter
from planner import make_planner
from auth import AuthService, AuthError, CredentialStore

DB_URL = os.environ.get("CHITRAGUPTA_DB", "sqlite:///chitragupta.db")
SessionMaker = make_session(DB_URL)


def _make_erp():
    """Real ERPNext if ERP_URL is set, else the in-memory stand-in.
    This one env var is the entire switch between demo and live."""
    if os.environ.get("ERP_URL"):
        from erp_frappe import FrappeERP
        erp = FrappeERP()
        print(f"[chitragupta] LIVE ERPNext at {erp.base} as {erp.ping()}")
        return erp
    print("[chitragupta] using FakeERP stand-in (set ERP_URL to go live)")
    return FakeERP()


default_erp = _make_erp()
def _user_adapter_factory(user, cred):
    """Build a per-user adapter from a bound ERP session (Phase B)."""
    from erp_frappe import FrappeERP
    if isinstance(cred, dict) and cred.get("sid"):
        return FrappeERP(session_sid=cred["sid"], csrf_token=cred.get("csrf"),
                         acting_user=user)
    return default_erp


creds = CredentialStore(default_erp,
                        adapter_factory=(_user_adapter_factory
                                         if os.environ.get("ERP_URL") else None))
auth = AuthService(SessionMaker)
_planner = make_planner()
engine = Engine(default_erp, SessionMaker, interpreter=make_interpreter(),
                planner=_planner)
print("[chitragupta] language brain:",
      "LLM (general, any doctype)" if _planner else
      "rules only — set ANTHROPIC_API_KEY for free-form questions")

BUILD_ID = "build-20260717-140906"

app = FastAPI(title="Chitragupta", version="0.4")


@app.get("/api/version")
def version():
    from erp_frappe import FrappeERP
    import os as _os
    base = _os.environ.get("CHITRAGUPTA_MODEL", "claude-sonnet-5")
    return {"build": BUILD_ID,
            "models": {"chat": base,
                       "agent": _os.environ.get("CHITRAGUPTA_MODEL_AGENT") or base,
                       "plan": _os.environ.get("CHITRAGUPTA_MODEL_PLAN") or base},
            "adapter_has_add_comment": hasattr(FrappeERP, "add_comment"),
            "create_uses": "quote/%20",
            "features": ["create","comment","report","voice","upload",
                         "conversation-memory","explain","analytics"]}

# CORS: the Desk widget runs on the ERPNext origin and calls this backend from
# the browser. Allow the ERP origin (and localhost for the portal).
_origins = [o for o in [
    os.environ.get("ERP_URL"),                 # e.g. http://192.168.179.128:8000
    os.environ.get("CHITRAGUPTA_ORIGIN"),      # any extra origin you need
    "http://127.0.0.1:8000", "http://localhost:8000",
] if o]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/embed/chitragupta_desk.js")
def desk_js():
    """Serve the Desk widget so ERPNext can include it by URL."""
    from fastapi.responses import Response
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "erpnext_embed", "chitragupta_desk.js")
    with open(path, encoding="utf-8") as f:
        return Response(f.read(), media_type="application/javascript")


# --------------------------------------------------------------- auth plumbing
def current_user(authorization: str | None = Header(default=None)) -> str:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    try:
        return auth.resolve(token)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


class LoginBody(BaseModel):
    user: str
    password: str


@app.post("/api/login")
def login(body: LoginBody):
    try:
        token = auth.login(body.user, body.password)
    except AuthError:
        s = SessionMaker()
        audit_log(s, "login_failed", body.user)
        raise HTTPException(status_code=401, detail="invalid credentials")
    s = SessionMaker()
    audit_log(s, "login", body.user)
    return {"token": token, "user": body.user,
            "full_name": USERS[body.user]["full_name"],
            "roles": USERS[body.user]["roles"]}


class SSOBody(BaseModel):
    user: str
    ts: str
    sig: str
    sid: str | None = None      # Phase B: the user's ERP session cookie
    csrf: str | None = None     # Frappe CSRF token (required for writes)


@app.post("/api/sso")
def sso(body: SSOBody):
    """Act-as-user Phase A: exchange an ERP-signed assertion for a backend
    session. The assertion comes from the chitragupta_desk Frappe app's
    whitelisted method, which runs AS the logged-in ERP user and signs with the
    shared secret (CHITRAGUPTA_SSO_SECRET on both sides)."""
    secret = os.environ.get("CHITRAGUPTA_SSO_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503,
                            detail="SSO not configured (CHITRAGUPTA_SSO_SECRET)")
    try:
        token = auth.login_sso(body.user, body.ts, body.sig, secret,
                               erp=default_erp)
    except AuthError as e:
        s = SessionMaker(); audit_log(s, "sso_failed", body.user)
        raise HTTPException(status_code=401, detail=str(e))
    s = SessionMaker(); audit_log(s, "sso_login", body.user)

    # ---- Phase B: bind the user's ERP session so actions EXECUTE as them ----
    creds.mark_sso(body.user)
    acting_mode = "bot"
    acting_reason = "no ERP session (sid) was provided by the widget"
    if body.sid and os.environ.get("ERP_URL"):
        try:
            from erp_frappe import FrappeERP
            probe = FrappeERP(session_sid=body.sid, csrf_token=body.csrf)
            whoami = probe._call("GET",
                                 "/api/method/frappe.auth.get_logged_user")
            owner = whoami if isinstance(whoami, str) else str(whoami)
            if owner == body.user:
                creds.register_session(body.user, body.sid, body.csrf)
                acting_mode = "user"
                acting_reason = "session verified and bound"
                audit_log(s, "erp_session_bound", body.user)
            else:
                acting_reason = f"session belongs to {owner!r}, not the asserted user"
                audit_log(s, "erp_session_mismatch", body.user, owner=owner)
        except Exception as e:
            acting_reason = f"session probe failed: {str(e)[:120]}"
            audit_log(s, "erp_session_probe_failed", body.user, error=str(e)[:120])

    from governance import USERS
    info = USERS.get(body.user, {})
    return {"token": token, "user": body.user,
            "full_name": info.get("full_name", body.user),
            "roles": info.get("roles", []),
            "acting_mode": acting_mode, "acting_reason": acting_reason}


@app.post("/api/logout")
def logout(authorization: str | None = Header(default=None)):
    token = authorization[7:] if authorization and \
        authorization.lower().startswith("bearer ") else ""
    return {"ok": auth.logout(token)}


@app.get("/api/users")
def users():
    """Demo helper for the login screen (password is 'demo' for all)."""
    return {"users": [{"id": u, **info} for u, info in USERS.items()]}


# ------------------------------------------------------------------- the loop
class Command(BaseModel):
    text: str
    context: dict | None = None
    file: dict | None = None       # {name, type, data(base64)} from the widget


class Decision(BaseModel):
    edits: dict | None = None


class Remember(BaseModel):
    key: str
    value: str


@app.post("/api/command/stream")
def command_stream(c: Command, user: str = Depends(current_user)):
    """SSE: emits {type:meta} once (activity/proposals/suggestions/download),
    then {type:delta, text} chunks of the reply, then {type:done}."""
    from fastapi.responses import StreamingResponse
    import json as _json

    def gen():
        engine.erp = creds.erp_for(user)
        out = engine.handle_command(c.text, user, c.context, c.file)
        meta = {k: out.get(k) for k in
                ("activity", "proposals", "suggestions", "download", "job_id")}
        yield "data: " + _json.dumps({"type": "meta", **meta},
                                     default=str) + "\n\n"
        reply = out.get("reply") or ""
        # chunk the reply so the client renders progressively
        step = 40
        for i in range(0, len(reply), step):
            yield "data: " + _json.dumps(
                {"type": "delta", "text": reply[i:i+step]}) + "\n\n"
        yield "data: " + _json.dumps({"type": "done"}) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/command")
def command(c: Command, user: str = Depends(current_user)):
    try:
        engine.erp = creds.erp_for(user)      # act-as-user binding
    except Exception as ex:
        if type(ex).__name__ == "UnboundSession":
            return {"reply": "Your secure session isn't connected yet — "
                             "please reload the ERP page and I'll act as you. "
                             "(Strict mode: I never fall back to shared "
                             "credentials.)",
                    "activity": [{"tag": "stop",
                                  "text": "strict SSO: unbound session"}],
                    "proposals": []}
        raise
    return engine.handle_command(c.text, user, c.context, c.file)


class Feedback(BaseModel):
    reply_excerpt: str
    rating: str          # "up" | "down"
    reason: str | None = None


@app.get("/api/erp-users")
def erp_users(q: str = "", user: str = Depends(current_user)):
    """Usernames for the @mention picker (best-effort; empty on failure)."""
    try:
        rows = engine.erp.search("User")
        names = [r.get("name") for r in rows if r.get("name")
                 and "@" in str(r.get("name"))]
        if q:
            names = [n for n in names if q.lower() in n.lower()]
        return {"users": sorted(names)[:8]}
    except Exception:
        return {"users": []}


@app.post("/api/feedback")
def feedback(f: Feedback, user: str = Depends(current_user)):
    """Thumbs up/down on an answer. Downs with reasons are gold: they audit as
    negative signals reviewable weekly alongside clean_rate."""
    s = SessionMaker()
    audit_log(s, "feedback", user, rating=f.rating,
              reason=(f.reason or "")[:300],
              excerpt=f.reply_excerpt[:200])
    return {"ok": True}


@app.get("/api/learning")
def learning(user: str = Depends(current_user)):
    """What Chitragupta has learned, and whether it's improving."""
    return engine.memory.learning_stats()


@app.get("/api/proposals")
def proposals(user: str = Depends(current_user)):
    return {"pending": engine.pending(user)}


@app.post("/api/proposals/{pid}/approve")
def approve(pid: int, d: Decision, user: str = Depends(current_user)):
    engine.erp = creds.erp_for(user)
    return engine.approve(pid, user, d.edits)


@app.post("/api/proposals/{pid}/reject")
def reject(pid: int, user: str = Depends(current_user)):
    return engine.reject(pid, user)


@app.get("/api/brief")
def brief(user: str = Depends(current_user)):
    return engine.brief(user)


@app.get("/api/memory")
def memory_list(user: str = Depends(current_user)):
    return {"memories": engine.memory.list_for_user(user)}


@app.post("/api/memory")
def memory_add(r: Remember, user: str = Depends(current_user)):
    engine.memory.remember_personal(user, r.key, r.value)
    return {"stored": True}


@app.delete("/api/memory/{mid}")
def memory_del(mid: int, user: str = Depends(current_user)):
    return {"deleted": engine.memory.forget(user, mid)}


@app.get("/api/skills")
def skills(user: str = Depends(current_user)):
    return {"skills": engine.memory.skills()}


@app.get("/api/audit")
def audit_trail(user: str = Depends(current_user)):
    s = SessionMaker()
    rows = s.query(AuditLog).order_by(AuditLog.id.desc()).limit(200).all()
    return {"audit": [{"ts": r.ts.isoformat(), "event": r.event, "actor": r.actor,
                       "detail": json.loads(r.detail_json)} for r in rows]}


# ------------------------------------------------------------------ frontends
def _serve(name: str) -> str:
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "frontend", name), encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def index():
    return _serve("index.html")


@app.get("/widget", response_class=HTMLResponse)
def widget():
    return _serve("widget.html")

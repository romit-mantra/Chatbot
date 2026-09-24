"""Authentication & sessions — identity comes from the server, never the client.

Before this module, the client sent `actor` in the request body (spoofable).
Now:
- POST /api/login {user, password} -> a random session token (stored hashed in DB)
- every API call sends Authorization: Bearer <token>
- the backend resolves the token to a user; body-supplied identity is IGNORED.

Act-as-user scaffolding:
- CredentialStore maps each Chitragupta user -> their ERP credential (Frappe
  API key:secret or OAuth token). `erp_for(user)` returns an adapter bound to
  that credential, so every ERP call can carry the real person's identity and
  ERPNext's permission engine becomes the hard backstop. With FakeERP the
  binding is a no-op (one shared instance); with FrappeAssistantAdapter it
  constructs a per-user client. This is the socket Frappe OAuth plugs into.

Demo users all have password "demo" (documented; replace with SSO/Frappe OAuth
in production — see docs/03).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import datetime as dt

from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from store import Base, now
from governance import USERS

SESSION_TTL_HOURS = 12
_PBKDF_ITER = 60_000


def _hash_pw(pw: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _PBKDF_ITER)


# Demo credential set: every user's password is "demo".
_SALT = b"chitragupta-demo-salt"
_PW_HASHES = {u: _hash_pw("demo", _SALT) for u in USERS}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)


class AuthError(RuntimeError):
    pass


class AuthService:
    def __init__(self, SessionMaker):
        self.S = SessionMaker

    def login(self, user: str, password: str) -> str:
        if user not in USERS:
            raise AuthError("invalid credentials")
        expected = _PW_HASHES[user]
        given = _hash_pw(password, _SALT)
        if not hmac.compare_digest(expected, given):
            raise AuthError("invalid credentials")
        token = secrets.token_urlsafe(32)
        s = self.S()
        s.add(Session(user=user, token_hash=_hash_token(token),
                      expires_at=now() + dt.timedelta(hours=SESSION_TTL_HOURS)))
        s.commit()
        return token

    def resolve(self, token: str | None) -> str:
        """Token -> user, or raise. THE ONLY SOURCE OF IDENTITY."""
        if not token:
            raise AuthError("missing token")
        s = self.S()
        row = s.query(Session).filter_by(token_hash=_hash_token(token)).first()
        if not row:
            raise AuthError("invalid token")
        exp = row.expires_at
        if exp.tzinfo is None:                      # SQLite strips tz
            exp = exp.replace(tzinfo=dt.timezone.utc)
        if exp < now():
            s.delete(row); s.commit()
            raise AuthError("session expired")
        return row.user

    def logout(self, token: str) -> bool:
        s = self.S()
        row = s.query(Session).filter_by(token_hash=_hash_token(token)).first()
        if not row:
            return False
        s.delete(row); s.commit()
        return True


class UnboundSession(Exception):
    """SSO user has no bound ERP session; strict mode refuses bot fallback."""
    def __init__(self, user):
        super().__init__(user)
        self.user = user


class CredentialStore:
    """Act-as-user scaffolding: user -> ERP credential -> bound adapter.

    Pilot mode: no per-user credentials registered -> everyone shares the
    default adapter (the single bot-user model, as planned for the pilot).
    Production: register each user's Frappe OAuth token / api key here (or a
    vault lookup) and erp_for() returns a per-user FrappeAssistantAdapter, so
    ERPNext enforces THEIR permissions natively.
    """

    def __init__(self, default_erp, adapter_factory=None):
        self.default_erp = default_erp
        self.adapter_factory = adapter_factory   # e.g. lambda cred: FrappeAssistantAdapter(url, cred)
        self.strict = os.environ.get("CHITRAGUPTA_STRICT_SSO", "0") == "1"
        self._sso_users: set[str] = set()
        self._creds: dict[str, str] = {}

    def register_session(self, user: str, sid: str, csrf: str | None) -> None:
        """Phase B: bind a VERIFIED ERP session to this user. erp_for() will
        then return an adapter that executes as that person."""
        self._creds[user] = {"sid": sid, "csrf": csrf or ""}

    def clear(self, user: str) -> None:
        self._creds.pop(user, None)

    def register(self, user: str, credential: str) -> None:
        self._creds[user] = credential

    def erp_for(self, user: str):
        cred = self._creds.get(user)
        if cred and self.adapter_factory:
            try:
                return self.adapter_factory(user, cred)
            except Exception:
                if self.strict:
                    raise UnboundSession(user)
                return self.default_erp      # degrade to the bot, never break
        if self.strict and user in self._sso_users:
            # act-as-user is the promise: an unbound SSO user gets NOTHING
            # through the service account — not even its narrow view.
            raise UnboundSession(user)
        return self.default_erp

    def mark_sso(self, user: str) -> None:
        self._sso_users.add(user)


# ----------------------------------------------------------- act-as-user SSO
# Replay window. VM guests drift (suspend/resume), so this is generous but
# still bounded, and overridable per-deployment.
SSO_MAX_AGE_SECONDS = int(os.environ.get("CHITRAGUPTA_SSO_MAX_AGE", "900"))


def diagnose_sso(user: str, ts: str, sig: str, secret: str) -> str:
    """Return '' if valid, else a precise reason. Used so a 401 tells us WHY
    (stale clock vs wrong secret) instead of forcing another guessing round."""
    if not secret:
        return "backend has no CHITRAGUPTA_SSO_SECRET"
    if not (user and ts and sig):
        return "assertion missing user/ts/sig"
    try:
        skew = dt.datetime.now(dt.timezone.utc).timestamp() - float(ts)
    except (TypeError, ValueError):
        return f"timestamp not a number: {ts!r}"
    if abs(skew) > SSO_MAX_AGE_SECONDS:
        return (f"clock skew {skew:+.0f}s exceeds {SSO_MAX_AGE_SECONDS}s "
                f"(ERP and backend clocks differ, or the assertion is stale)")
    expected = hmac.new(secret.encode(), f"{user}|{ts}".encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return (f"signature mismatch: the backend secret differs from the "
                f"site's (backend secret length={len(secret)}, "
                f"starts {secret[:3]!r}, ends {secret[-3:]!r})")
    return ""


def verify_sso_assertion(user: str, ts: str, sig: str, secret: str) -> bool:
    """Verify an assertion produced by the chitragupta_desk Frappe app.

    The ERP-side method runs AS the logged-in user and signs
    HMAC_SHA256(secret, f"{user}|{ts}"). Because only the ERP server and this
    backend know the secret, a valid signature proves the ERP vouches that
    `user` is genuinely logged in. The timestamp bounds replay."""
    if not (user and ts and sig and secret):
        return False
    try:
        age = abs(dt.datetime.now(dt.timezone.utc).timestamp() - float(ts))
    except (TypeError, ValueError):
        return False
    if age > SSO_MAX_AGE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{user}|{ts}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def fetch_erp_roles(erp, user_email: str) -> tuple[str, list[str]]:
    """Read the user's full name and REAL ERPNext roles via the bot connection.
    Requires the bot to be able to read the User doctype; degrades to an empty
    role list (write-gate grants nothing; ERPNext still rules its own side)."""
    try:
        doc = erp.get("User", user_email)
        roles = [r.get("role") for r in (doc.get("roles") or []) if r.get("role")]
        # ERPNext's Administrator usually has an EMPTY roles table — its access
        # is implicit, not granted via role rows. Without this, the assistant
        # sees no roles and guesses (it claimed "Purchase User" for an admin).
        if user_email.lower() in ("administrator", "admin") or (
                not roles and doc.get("user_type") == "System User"
                and user_email.lower() == "administrator"):
            roles = sorted(set(roles) | {"Administrator", "System Manager"})
        return doc.get("full_name") or user_email, roles
    except Exception:
        return user_email, []


class AuthService(AuthService):  # extend in place
    def login_sso(self, user: str, ts: str, sig: str, secret: str,
                  erp=None) -> str:
        from governance import USERS, register_user
        why = diagnose_sso(user, ts, sig, secret)
        if why:
            raise AuthError(f"invalid SSO assertion — {why}")
        if user not in USERS:
            full_name, roles = fetch_erp_roles(erp, user) if erp else (user, [])
            register_user(user, full_name, roles)
        token = secrets.token_urlsafe(32)
        s = self.S()
        s.add(Session(user=user, token_hash=_hash_token(token),
                      expires_at=now() + dt.timedelta(hours=SESSION_TTL_HOURS)))
        s.commit()
        return token

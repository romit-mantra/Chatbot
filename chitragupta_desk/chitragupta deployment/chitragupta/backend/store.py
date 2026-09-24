"""Persistence: jobs, proposals, audit trail, and the three memories.

Memories (see docs/05-memory.md):
- Memory(kind="personal", owner=user)  : per-user preferences. Never crosses users.
- Memory(kind="org", scope_doctype=...) : company norms, learned from approved
  outcomes and human edits. Recall is filtered by the current user's permissions.
- Skill : an approved multi-step task saved as a replayable recipe; vetted flag
  gates unsupervised use.
Confidence: reinforced on confirmation, decayed on contradiction (recency wins
only after the user confirms — conflicts surface as questions, not silent picks).
"""

from __future__ import annotations

import datetime as dt
import json
from sqlalchemy import create_engine, String, Integer, Float, Text, DateTime, Boolean
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            scoped_session, sessionmaker)


class Base(DeclarativeBase):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="open")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(120), default="")
    doctype: Mapped[str] = mapped_column(String(120))
    draft_name: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(40))
    value: Mapped[float] = mapped_column(Float, default=0.0)
    tier: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json or "{}")


class AuditLog(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    event: Mapped[str] = mapped_column(String(60))
    actor: Mapped[str] = mapped_column(String(120))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")


class Memory(Base):
    __tablename__ = "memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20))            # personal | org
    owner: Mapped[str] = mapped_column(String(120), default="")   # user (personal)
    scope_doctype: Mapped[str] = mapped_column(String(120), default="")  # org scope
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(40), default="explicit")  # explicit|learned
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    doctype: Mapped[str] = mapped_column(String(120))
    runs: Mapped[int] = mapped_column(Integer, default=0)
    approved_clean: Mapped[int] = mapped_column(Integer, default=0)  # approved w/o edits
    vetted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


def make_session(url: str = "sqlite:///chitragupta.db"):
    """Session factory.

    Uses a scoped_session so each thread reuses ONE connection instead of
    opening a new one per call. Without this, code that does `s = Session()`
    without closing leaks connections and exhausts the pool
    (`QueuePool limit of size 5 overflow 10 reached`) after ~15 requests.
    scoped_session makes repeated Session() calls in the same thread return the
    SAME session, so the leak cannot happen.
    """
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(pool_size=20, max_overflow=30, pool_recycle=1800)
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return scoped_session(factory)


def audit(session, event: str, actor: str, **detail) -> None:
    session.add(AuditLog(event=event, actor=actor, detail_json=json.dumps(detail)))
    session.commit()

"""Relational store (PostgreSQL in production, SQLite locally).

This is the system of record: every run, source, crawl decision, claim, verdict, conflict,
graph episode and chat turn lives here. It answers "where did this come from?" with joins,
and it is the audit trail that proves what the system did and did not do.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, AsyncIterator

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    city: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(200), default="")
    city_slug: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")  # queued|running|done|failed
    stage: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    graph_status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    node: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    round: Mapped[int] = mapped_column(Integer, default=1)
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str] = mapped_column(Text, default="")
    domain: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text, default="")
    query: Mapped[str] = mapped_column(Text, default="")
    dimension_hint: Mapped[str] = mapped_column(String(64), default="")
    search_snippet: Mapped[str] = mapped_column(Text, default="")
    credibility_tier: Mapped[int] = mapped_column(Integer, default=3)
    credibility_reason: Mapped[str] = mapped_column(Text, default="")
    # crawlability gate
    crawl_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    crawl_reason: Mapped[str] = mapped_column(Text, default="")
    crawl_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # fetch
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    # candidate|blocked|fetched|fetch_failed|empty|processed
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_date: Mapped[str] = mapped_column(String(32), default="")
    content_type: Mapped[str] = mapped_column(String(64), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    text_chars: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)  # human-friendly C-number within the run
    dimension: Mapped[str] = mapped_column(String(64))
    claim_type: Mapped[str] = mapped_column(String(32))  # statistic|programme|policy|organisation|person|fact
    statement: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text)
    metric_key: Mapped[str] = mapped_column(String(64), default="")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(64), default="")
    year: Mapped[str] = mapped_column(String(16), default="")
    geography_level: Mapped[str] = mapped_column(String(32), default="unknown")
    geography_name: Mapped[str] = mapped_column(String(200), default="")
    entities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # verification
    quote_found: Mapped[bool] = mapped_column(Boolean, default=False)
    verdict: Mapped[str] = mapped_column(String(32), default="pending")
    # pending|supported|partially_supported|unsupported
    verdict_reason: Mapped[str] = mapped_column(Text, default="")
    checker: Mapped[str] = mapped_column(String(64), default="")
    not_city_level: Mapped[bool] = mapped_column(Boolean, default=False)
    conflict_group: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Conflict(Base):
    __tablename__ = "conflicts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    metric_key: Mapped[str] = mapped_column(String(64))
    geography_level: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))  # conflict|time_series
    claim_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text)


class GraphEpisode(Base):
    __tablename__ = "graph_episodes"
    episode_uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    city_slug: Mapped[str] = mapped_column(String(200), index=True)
    claim_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    n_nodes: Mapped[int] = mapped_column(Integer, default=0)
    n_edges: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def configure(url: str) -> None:
    """Point the module at another database (used by tests)."""
    global engine, SessionLocal
    engine = create_async_engine(url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def session() -> AsyncSession:
    return SessionLocal()


async def log_event(run_id: str, node: str, message: str, level: str = "info") -> None:
    async with session() as s:
        s.add(RunEvent(run_id=run_id, node=node, message=message, level=level))
        await s.commit()


async def set_stage(run_id: str, stage: str, **fields: Any) -> None:
    async with session() as s:
        run = await s.get(Run, run_id)
        if run is None:
            return
        run.stage = stage
        for k, v in fields.items():
            setattr(run, k, v)
        await s.commit()


async def claims_for_run(run_id: str, verdicts: tuple[str, ...] | None = None) -> list[Claim]:
    async with session() as s:
        q = select(Claim).where(Claim.run_id == run_id).order_by(Claim.seq)
        if verdicts:
            q = q.where(Claim.verdict.in_(verdicts))
        return list((await s.execute(q)).scalars())


async def sources_for_run(run_id: str) -> list[Source]:
    async with session() as s:
        q = select(Source).where(Source.run_id == run_id).order_by(Source.credibility_tier, Source.domain)
        return list((await s.execute(q)).scalars())


async def iter_session() -> AsyncIterator[AsyncSession]:  # FastAPI dependency
    async with session() as s:
        yield s

"""FastAPI application: API + single-page UI."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import chat as chat_mod
from . import db, report
from .config import settings
from .graphstore import GraphStore
from .llm import LLM
from .taxonomy import DIMENSIONS
from .tools.crawl import CrawlabilityAgent, Fetcher
from .tools.search import TavilySearch
from .vectorstore import VectorStore
from .workflow import Deps, execute_run, slugify, workflow_mermaid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("c4c")
STATIC = Path(__file__).parent / "static"
ACCESS_CODE = os.getenv("ACCESS_CODE", "")
RUN_SLOTS = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_RUNS", "2")))

state: dict[str, Any] = {}


def make_deps() -> Deps:
    return Deps(llm=LLM(), search=TavilySearch(), crawler=CrawlabilityAgent(), fetcher=Fetcher(),
                vectors=state["vectors"], graph=state["graph"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    # runs interrupted by a restart are marked as such rather than left "running" forever
    async with db.session() as s:
        for r in (await s.execute(select(db.Run).where(db.Run.status.in_(("running", "queued"))))).scalars():
            r.status, r.error, r.finished_at = "failed", "Interrupted by a service restart", db.now()
        await s.commit()
    state["vectors"] = VectorStore()
    state["graph"] = GraphStore()
    state["llm"] = LLM()
    try:
        await state["graph"].init()
    except Exception as exc:  # noqa: BLE001
        log.error("Graph init failed: %s", exc)
    state["tasks"] = set()
    yield
    await state["graph"].close()


app = FastAPI(title="CARDIO4Cities City Intelligence", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _guard(code: str | None) -> None:
    if ACCESS_CODE and code != ACCESS_CODE:
        raise HTTPException(401, "Access code required")


class NewRun(BaseModel):
    city: str = Field(min_length=2, max_length=120)
    country: str = Field(default="", max_length=120)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    out: dict[str, Any] = {"relational": "ok", "vector": "unknown", "graph": "disabled",
                           "llm_key": bool(settings.openai_api_key), "search_key": bool(settings.tavily_api_key),
                           "access_code_required": bool(ACCESS_CODE)}
    try:
        await state["vectors"].ensure()
        out["vector"] = "qdrant-cloud" if settings.qdrant_url else "qdrant-local"
    except Exception as exc:  # noqa: BLE001
        out["vector"] = f"error: {type(exc).__name__}"
    g = state["graph"]
    if g.enabled:
        out["graph"] = "neo4j+graphiti" if g.g is not None else "configured, not connected"
    return out


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    return {"dimensions": [{"key": d.key, "title": d.title, "why": d.why, "questions": d.key_questions}
                           for d in DIMENSIONS],
            "access_code_required": bool(ACCESS_CODE),
            "models": {"research": settings.research_model, "checker": settings.checker_model,
                       "graph": settings.graph_model}}


@app.get("/api/workflow", response_class=PlainTextResponse)
async def workflow() -> str:
    return workflow_mermaid()


async def _run_task(run_id: str) -> None:
    async with RUN_SLOTS:
        await execute_run(run_id, make_deps())


@app.post("/api/runs")
async def create_run(body: NewRun, x_access_code: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_access_code)
    async with db.session() as s:
        run = db.Run(city=body.city.strip(), country=body.country.strip(),
                     city_slug=slugify(body.city, body.country), status="queued")
        s.add(run)
        await s.commit()
        run_id = run.id
    await db.log_event(run_id, "queue", f"Research requested for {body.city}")
    t = asyncio.create_task(_run_task(run_id))
    state["tasks"].add(t)
    t.add_done_callback(state["tasks"].discard)
    return {"id": run_id}


def _run_json(r: db.Run, full: bool = True) -> dict[str, Any]:
    out = {"id": r.id, "city": r.city, "country": r.country, "city_slug": r.city_slug, "status": r.status,
           "stage": r.stage, "created_at": r.created_at.isoformat(),
           "finished_at": r.finished_at.isoformat() if r.finished_at else None,
           "graph_status": r.graph_status, "error": r.error, "stats": r.stats}
    if full:
        out.update({"plan": r.plan, "coverage": r.coverage, "brief": r.brief})
    return out


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    async with db.session() as s:
        runs = (await s.execute(select(db.Run).order_by(db.Run.created_at.desc()).limit(50))).scalars()
        return [_run_json(r, full=False) for r in runs]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    async with db.session() as s:
        r = await s.get(db.Run, run_id)
        if not r:
            raise HTTPException(404)
        return _run_json(r)


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, after: int = 0) -> list[dict[str, Any]]:
    async with db.session() as s:
        rows = (await s.execute(select(db.RunEvent).where(db.RunEvent.run_id == run_id, db.RunEvent.id > after)
                                .order_by(db.RunEvent.id))).scalars()
        return [{"id": e.id, "ts": e.ts.isoformat(), "node": e.node, "level": e.level, "message": e.message}
                for e in rows]


@app.get("/api/runs/{run_id}/claims")
async def claims(run_id: str) -> list[dict[str, Any]]:
    async with db.session() as s:
        rows = list((await s.execute(select(db.Claim).where(db.Claim.run_id == run_id).order_by(db.Claim.seq))).scalars())
        srcs = {x.id: x for x in (await s.execute(select(db.Source).where(db.Source.run_id == run_id))).scalars()}
    return [{"id": c.id, "seq": c.seq, "dimension": c.dimension, "type": c.claim_type, "statement": c.statement,
             "quote": c.quote, "metric_key": c.metric_key, "value": c.value, "unit": c.unit, "year": c.year,
             "geography_level": c.geography_level, "geography_name": c.geography_name,
             "not_city_level": c.not_city_level, "verdict": c.verdict, "verdict_reason": c.verdict_reason,
             "checker": c.checker, "conflict_group": c.conflict_group, "entities": c.entities,
             "source": {"id": c.source_id, "url": srcs[c.source_id].url, "domain": srcs[c.source_id].domain,
                        "title": srcs[c.source_id].title, "tier": srcs[c.source_id].credibility_tier,
                        "published": srcs[c.source_id].published_date}}
            for c in rows]


@app.get("/api/runs/{run_id}/sources")
async def sources(run_id: str) -> list[dict[str, Any]]:
    rows = await db.sources_for_run(run_id)
    return [{"id": x.id, "round": x.round, "url": x.url, "domain": x.domain, "title": x.title, "query": x.query,
             "dimension_hint": x.dimension_hint, "tier": x.credibility_tier, "tier_reason": x.credibility_reason,
             "crawl_allowed": x.crawl_allowed, "crawl_reason": x.crawl_reason, "status": x.status,
             "http_status": x.http_status, "published": x.published_date, "chars": x.text_chars, "error": x.error,
             "fetched_at": x.fetched_at.isoformat() if x.fetched_at else None} for x in rows]


@app.get("/api/runs/{run_id}/conflicts")
async def conflicts(run_id: str) -> list[dict[str, Any]]:
    async with db.session() as s:
        rows = (await s.execute(select(db.Conflict).where(db.Conflict.run_id == run_id))).scalars()
        return [{"id": c.id, "metric_key": c.metric_key, "geography_level": c.geography_level, "kind": c.kind,
                 "claim_ids": c.claim_ids, "description": c.description} for c in rows]


@app.get("/api/runs/{run_id}/graph")
async def graph(run_id: str) -> dict[str, Any]:
    async with db.session() as s:
        r = await s.get(db.Run, run_id)
        if not r:
            raise HTTPException(404)
        eps = {e.episode_uuid: e.source_id for e in (await s.execute(
            select(db.GraphEpisode).where(db.GraphEpisode.city_slug == r.city_slug))).scalars()}
        srcs = {x.id: {"url": x.url, "domain": x.domain, "title": x.title} for x in (await s.execute(
            select(db.Source).where(db.Source.id.in_(set(eps.values()))))).scalars()} if eps else {}
    g = state["graph"]
    if not g.enabled:
        return {"enabled": False, "nodes": [], "edges": []}
    try:
        sub = await g.subgraph(r.city_slug)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"enabled": True, "error": str(exc)[:300], "nodes": [], "edges": []}, status_code=200)
    for e in sub["edges"]:
        e["sources"] = [srcs[eps[x]] for x in e["episodes"] if x in eps and eps[x] in srcs]
    return {"enabled": True, **sub}


@app.get("/api/claims/{claim_id}/provenance")
async def claim_provenance(claim_id: str) -> dict[str, Any]:
    p = await chat_mod.provenance(claim_id)
    if not p:
        raise HTTPException(404)
    return p


@app.get("/api/runs/{run_id}/chat")
async def chat_history(run_id: str) -> list[dict[str, Any]]:
    async with db.session() as s:
        rows = (await s.execute(select(db.ChatMessage).where(db.ChatMessage.run_id == run_id)
                                .order_by(db.ChatMessage.id))).scalars()
        return [{"role": m.role, "content": m.content, "citations": m.citations} for m in rows]


@app.post("/api/runs/{run_id}/chat")
async def ask(run_id: str, body: Question, x_access_code: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_access_code)
    async with db.session() as s:
        r = await s.get(db.Run, run_id)
        if not r:
            raise HTTPException(404)
    return await chat_mod.answer(run_id=run_id, question=body.question, llm=state["llm"],
                                 vectors=state["vectors"], graph=state["graph"])


@app.get("/api/runs/{run_id}/report.{fmt}")
async def download_report(run_id: str, fmt: str) -> Response:
    data = await report.collect(run_id)
    if not data:
        raise HTTPException(404)
    blocks = report.build_blocks(data)
    if fmt == "md":
        return Response(report.to_markdown(blocks), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{report.filename(data["run"], "md")}"'})
    if fmt == "docx":
        return Response(report.to_docx(blocks),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="{report.filename(data["run"], "docx")}"'})
    raise HTTPException(400, "fmt must be md or docx")

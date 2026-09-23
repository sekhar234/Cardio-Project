"""The orchestrated research workflow (LangGraph).

    plan -> search -> crawl_gate -> fetch -> extract -> fact_check -> assess_coverage
                ^                                                         |
                |------------------ followup_plan <----- (gaps & budget) -+
                                                                          | (sufficient or budget spent)
                                                                          v
                                   resolve_conflicts -> build_graph -> synthesize -> finalize

State passed between nodes is small (ids, counters, the plan); the heavy data lives in Postgres,
so each node is restartable and inspectable, and the UI can show progress from the database.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langchain_core.runnables import RunnableConfig
from sqlalchemy import func, select

from . import db
from .agents import conflicts as conflicts_agent
from .agents import coverage as coverage_agent
from .agents.extractor import extract_claims
from .agents.factchecker import check_source_claims, claim_key
from .agents.planner import make_followups, make_plan
from .agents.synthesizer import synthesize
from .config import settings
from .graphstore import GraphProtocol
from .llm import LLMProtocol
from .tools.crawl import CrawlabilityAgent, FetcherProtocol, credibility, domain_of, relevant_excerpt
from .tools.search import SearchProtocol
from .vectorstore import VectorStore, chunk_text

log = logging.getLogger(__name__)


def slugify(*parts: str) -> str:
    s = "-".join(p for p in parts if p)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:120] or "city"


def canonical_url(url: str) -> str:
    url = re.sub(r"#.*$", "", url.strip())
    url = re.sub(r"[?&](utm_[^=&]+|fbclid|gclid)=[^&]*", "", url)
    return url.rstrip("/")


@dataclass
class Deps:
    llm: LLMProtocol
    search: SearchProtocol
    crawler: CrawlabilityAgent
    fetcher: FetcherProtocol
    vectors: VectorStore
    graph: GraphProtocol


class ResearchState(TypedDict, total=False):
    run_id: str
    city: str
    country: str
    city_slug: str
    round: int
    queries: list[dict[str, str]]
    tried_queries: list[str]
    new_source_ids: list[str]
    coverage: dict[str, Any]
    gaps: dict[str, list[str]]


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


async def _gather_limited(coros: list, limit: int) -> list:
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)


# --- nodes -----------------------------------------------------------------------------------------
async def plan_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id = state["run_id"]
    await db.set_stage(run_id, "plan", status="running")
    await db.log_event(run_id, "plan", f"Planning research for {state['city']}")
    plan = await make_plan(d.llm, state["city"], state.get("country", ""), settings.max_queries_per_round)
    city, country = plan.get("city") or state["city"], plan.get("country") or state.get("country", "")
    slug = slugify(city, country)
    await db.set_stage(run_id, "plan", plan=plan, city_slug=slug, country=country)
    note = f" Note: {plan['ambiguity_note']}" if plan.get("ambiguity_note") else ""
    await db.log_event(run_id, "plan",
                       f"Resolved to {city}, {country}. {len(plan['queries'])} queries across 7 dimensions "
                       f"(languages: {', '.join(plan.get('local_languages', [])[:3]) or 'en'}).{note}")
    return {"city": city, "country": country, "city_slug": slug, "round": 1, "queries": plan["queries"],
            "tried_queries": [q["query"] for q in plan["queries"]]}


async def search_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id, rnd = state["run_id"], state["round"]
    await db.set_stage(run_id, f"search (round {rnd})")
    queries = state.get("queries", [])
    results = await _gather_limited([d.search.search(q["query"], 6) for q in queries], 4)

    async with db.session() as s:
        existing = {canonical_url(u) for u in (await s.execute(
            select(db.Source.url).where(db.Source.run_id == run_id))).scalars()}

    candidates: dict[str, dict[str, Any]] = {}
    failures = 0
    for q, res in zip(queries, results):
        if isinstance(res, Exception):
            failures += 1
            continue
        for rank, r in enumerate(res):
            url = canonical_url(r["url"])
            if url in existing or url in candidates:
                continue
            tier, why = credibility(url)
            candidates[url] = {**r, "url": url, "query": q["query"], "dimension": q["dimension"],
                               "tier": tier, "tier_reason": why, "rank": rank}

    # Select: prefer credible sources, keep every dimension represented, cap per-domain dominance.
    ordered = sorted(candidates.values(), key=lambda c: (c["tier"], c["rank"], -c.get("score", 0)))
    picked: list[dict[str, Any]] = []
    per_dim: dict[str, int] = {}
    per_domain: dict[str, int] = {}
    budget = settings.max_sources_per_round
    for pass_no in (0, 1):
        for c in ordered:
            if len(picked) >= budget or c in picked:
                continue
            dom = domain_of(c["url"])
            if per_domain.get(dom, 0) >= 3:
                continue
            if pass_no == 0 and per_dim.get(c["dimension"], 0) >= max(2, budget // 7):
                continue
            picked.append(c)
            per_dim[c["dimension"]] = per_dim.get(c["dimension"], 0) + 1
            per_domain[dom] = per_domain.get(dom, 0) + 1

    ids = []
    async with db.session() as s:
        for c in picked:
            src = db.Source(run_id=run_id, round=rnd, url=c["url"], domain=domain_of(c["url"]),
                            title=c.get("title", "")[:500], query=c["query"], dimension_hint=c["dimension"],
                            search_snippet=c.get("snippet", ""), credibility_tier=c["tier"],
                            credibility_reason=c["tier_reason"])
            s.add(src)
            await s.flush()
            ids.append(src.id)
        await s.commit()
    await db.log_event(run_id, "search",
                       f"Round {rnd}: {len(queries)} queries → {len(candidates)} new URLs; selected {len(ids)} "
                       f"(tier-1: {sum(1 for c in picked if c['tier'] == 1)})"
                       + (f"; {failures} searches failed" if failures else ""))
    return {"new_source_ids": ids}


async def crawl_gate_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id = state["run_id"]
    await db.set_stage(run_id, "crawlability check")
    async with db.session() as s:
        sources = list((await s.execute(select(db.Source).where(db.Source.id.in_(state["new_source_ids"])))).scalars())
        decisions = await _gather_limited([d.crawler.check(src.url) for src in sources], 8)
        allowed = 0
        for src, dec in zip(sources, decisions):
            if isinstance(dec, Exception):
                src.crawl_allowed, src.crawl_reason = False, f"Crawlability check failed: {type(dec).__name__}"
            else:
                src.crawl_allowed, src.crawl_reason = dec.allowed, dec.reason
            src.crawl_checked_at = db.now()
            src.status = "candidate" if src.crawl_allowed else "blocked"
            allowed += bool(src.crawl_allowed)
        await s.commit()
    await db.log_event(run_id, "crawl_gate", f"{allowed}/{len(sources)} sources permit automated extraction; "
                                             f"{len(sources) - allowed} blocked before any request to the page")
    return {}


async def fetch_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id, city = state["run_id"], state["city"]
    await db.set_stage(run_id, "fetch")
    async with db.session() as s:
        sources = list((await s.execute(select(db.Source).where(
            db.Source.id.in_(state["new_source_ids"]), db.Source.crawl_allowed.is_(True)))).scalars())

    async def fetch_one(src: db.Source):
        res = await d.fetcher.fetch(src.url)
        if res.final_url and domain_of(res.final_url) != src.domain:
            dec = await d.crawler.check(res.final_url)  # redirected elsewhere: re-check permission
            if not dec.allowed:
                res.ok, res.text, res.error = False, "", f"Redirected to {domain_of(res.final_url)}: {dec.reason}"
        return res

    results = await _gather_limited([fetch_one(src) for src in sources], 6)
    ok = 0
    index_items, index_texts = [], []
    async with db.session() as s:
        for src, res in zip(sources, results):
            row = await s.get(db.Source, src.id)
            if isinstance(res, Exception):
                row.status, row.error = "fetch_failed", f"{type(res).__name__}"
                continue
            row.http_status, row.final_url, row.content_type = res.status, res.final_url, res.content_type
            row.fetched_at = db.now()
            if not res.ok:
                row.status, row.error = "fetch_failed", res.error
                continue
            ok += 1
            row.status, row.text, row.text_chars = "fetched", res.text[:200_000], len(res.text)
            row.title = res.title or row.title
            row.published_date = res.published or ""
            for i, chunk in enumerate(chunk_text(relevant_excerpt(res.text, city, 30_000))[:25]):
                index_items.append({"key": f"passage:{src.id}:{i}", "kind": "passage", "city_slug": state["city_slug"],
                                    "run_id": run_id, "source_id": src.id, "url": src.url,
                                    "title": row.title[:200], "text": chunk})
                index_texts.append(chunk)
        await s.commit()
    if index_items:
        try:
            await d.vectors.upsert(index_items, await d.llm.embed(index_texts))
        except Exception as exc:  # noqa: BLE001
            await db.log_event(run_id, "fetch", f"Passage indexing failed: {exc}", "warning")
    await db.log_event(run_id, "fetch", f"Fetched {ok}/{len(sources)} permitted sources; "
                                        f"indexed {len(index_items)} passages")
    return {}


async def extract_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id, city, country = state["run_id"], state["city"], state.get("country", "")
    await db.set_stage(run_id, "extract")
    async with db.session() as s:
        sources = list((await s.execute(select(db.Source).where(
            db.Source.id.in_(state["new_source_ids"]), db.Source.status == "fetched"))).scalars())
    results = await _gather_limited([
        extract_claims(d.llm, city=city, country=country, title=src.title, url=src.url,
                       text=relevant_excerpt(src.text, city, settings.max_source_chars))
        for src in sources], settings.llm_concurrency)
    n = 0
    async with db.session() as s:
        seq = (await s.execute(select(func.max(db.Claim.seq)).where(db.Claim.run_id == run_id))).scalar() or 0
        for src, claims in zip(sources, results):
            row = await s.get(db.Source, src.id)
            if isinstance(claims, Exception):
                row.error = f"Extraction failed: {type(claims).__name__}"
                continue
            row.status = "processed" if claims else "empty"
            for c in claims:
                seq += 1
                n += 1
                s.add(db.Claim(run_id=run_id, source_id=src.id, seq=seq, **c))
        await s.commit()
    await db.log_event(run_id, "extract", f"Extracted {n} candidate claims from {len(sources)} sources "
                                          "(pending independent verification)")
    return {}


async def fact_check_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id, city, country = state["run_id"], state["city"], state.get("country", "")
    await db.set_stage(run_id, "fact check")
    async with db.session() as s:
        pending = list((await s.execute(select(db.Claim).where(
            db.Claim.run_id == run_id, db.Claim.verdict == "pending"))).scalars())
        src_ids = {c.source_id for c in pending}
        sources = {x.id: x for x in (await s.execute(select(db.Source).where(db.Source.id.in_(src_ids)))).scalars()}

    by_src: dict[str, list[db.Claim]] = {}
    for c in pending:
        by_src.setdefault(c.source_id, []).append(c)

    def as_dict(c: db.Claim) -> dict[str, Any]:
        return {"id": c.id, "statement": c.statement, "quote": c.quote, "value": c.value, "year": c.year,
                "geography_level": c.geography_level, "geography_name": c.geography_name}

    results = await _gather_limited([
        check_source_claims(d.llm, city=city, country=country, source_text=sources[sid].text,
                            claims=[as_dict(c) for c in cs])
        for sid, cs in by_src.items()], settings.llm_concurrency)

    tally = {"supported": 0, "partially_supported": 0, "unsupported": 0}
    verified_items, verified_texts = [], []
    async with db.session() as s:
        for (sid, cs), res in zip(by_src.items(), results):
            for c in cs:
                row = await s.get(db.Claim, c.id)
                if isinstance(res, Exception) or c.id not in res:
                    row.verdict, row.verdict_reason, row.checker = "unsupported", \
                        "Fact check could not be completed; excluded by default.", "error"
                else:
                    v = res[c.id]
                    row.verdict, row.verdict_reason, row.checker = v["verdict"], v["reason"], v["checker"]
                    row.quote_found, row.not_city_level = v["quote_found"], v["not_city_level"]
                    if v.get("evidence_geography_level") and v["not_city_level"]:
                        row.geography_level = v["evidence_geography_level"]
                tally[row.verdict] = tally.get(row.verdict, 0) + 1
                if row.verdict in ("supported", "partially_supported"):
                    verified_items.append({"key": claim_key(row.id), "kind": "claim", "city_slug": state["city_slug"],
                                           "run_id": run_id, "claim_id": row.id, "seq": row.seq,
                                           "source_id": row.source_id, "verdict": row.verdict,
                                           "dimension": row.dimension, "text": row.statement})
                    verified_texts.append(row.statement)
        await s.commit()
    if verified_items:
        try:
            await d.vectors.upsert(verified_items, await d.llm.embed(verified_texts))
        except Exception as exc:  # noqa: BLE001
            await db.log_event(run_id, "fact_check", f"Claim indexing failed: {exc}", "warning")
    await db.log_event(run_id, "fact_check",
                       f"Independent check: {tally['supported']} supported, {tally['partially_supported']} "
                       f"partially supported, {tally['unsupported']} rejected (excluded from graph, report and chat)")
    return {}


async def assess_coverage_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id = state["run_id"]
    await db.set_stage(run_id, "coverage assessment")
    claims = await db.claims_for_run(run_id, ("supported", "partially_supported"))
    report = await coverage_agent.assess(d.llm, state["city"], [
        {"dimension": c.dimension, "statement": c.statement, "not_city_level": c.not_city_level} for c in claims])
    gaps = coverage_agent.gaps_from(report)
    await db.set_stage(run_id, "coverage assessment", coverage=report)
    summary = ", ".join(f"{k}: {v['status']}" for k, v in report.items())
    will_loop = bool(gaps) and state["round"] < settings.max_research_rounds
    await db.log_event(run_id, "assess_coverage",
                       f"Coverage after round {state['round']}: {summary}. "
                       + ("Research continues on the gaps." if will_loop else
                          ("Budget reached: remaining gaps recorded as unknowns." if gaps else "Sufficient.")))
    return {"coverage": report, "gaps": gaps}


def route_after_coverage(state: ResearchState) -> str:
    if state.get("gaps") and state["round"] < settings.max_research_rounds:
        return "followup_plan"
    return "resolve_conflicts"


async def followup_plan_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id = state["run_id"]
    qs = await make_followups(d.llm, state["city"], state.get("country", ""), state["gaps"],
                              state.get("tried_queries", []), max(4, settings.max_queries_per_round // 2))
    await db.log_event(run_id, "followup_plan", f"{len(qs)} targeted follow-up queries for "
                                                f"{', '.join(state['gaps'].keys())}")
    return {"round": state["round"] + 1, "queries": qs,
            "tried_queries": state.get("tried_queries", []) + [q["query"] for q in qs]}


async def resolve_conflicts_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    run_id = state["run_id"]
    await db.set_stage(run_id, "conflict resolution")
    claims = await db.claims_for_run(run_id, ("supported", "partially_supported"))
    found = conflicts_agent.find_conflicts([
        {"id": c.id, "seq": c.seq, "claim_type": c.claim_type, "metric_key": c.metric_key, "value": c.value,
         "unit": c.unit, "year": c.year, "geography_level": c.geography_level, "not_city_level": c.not_city_level}
        for c in claims])
    async with db.session() as s:
        for f in found:
            conf = db.Conflict(run_id=run_id, **f)
            s.add(conf)
            await s.flush()
            if f["kind"] == "conflict":
                for cid in f["claim_ids"]:
                    (await s.get(db.Claim, cid)).conflict_group = conf.id
        await s.commit()
    n_conf = sum(1 for f in found if f["kind"] == "conflict")
    await db.log_event(run_id, "resolve_conflicts",
                       f"{n_conf} conflicting statistics flagged; {len(found) - n_conf} time series ordered")
    return {}


def _ref_time(src: db.Source) -> dt.datetime:
    if src.published_date:
        try:
            return dt.datetime.fromisoformat(src.published_date[:10]).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return src.fetched_at or db.now()


async def build_graph_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id, city, country = state["run_id"], state["city"], state.get("country", "")
    await db.set_stage(run_id, "knowledge graph", graph_status="building")
    if not d.graph.enabled:
        await db.set_stage(run_id, "knowledge graph", graph_status="disabled")
        await db.log_event(run_id, "build_graph", "Graph store not configured (NEO4J_* missing)", "warning")
        return {}
    claims = await db.claims_for_run(run_id, ("supported", "partially_supported"))
    by_src: dict[str, list[db.Claim]] = {}
    for c in claims:
        by_src.setdefault(c.source_id, []).append(c)
    async with db.session() as s:
        sources = {x.id: x for x in (await s.execute(select(db.Source).where(db.Source.id.in_(by_src)))).scalars()}

    added = failed = edges = 0
    # Sequential on purpose: Graphiti resolves entities against what is already in the graph,
    # so ordering episodes (most credible sources first) improves deduplication.
    for sid in sorted(by_src, key=lambda k: (sources[k].credibility_tier, sources[k].domain)):
        src, cs = sources[sid], by_src[sid]
        lines = []
        for c in cs:
            geo = f" (applies to {c.geography_level} level: {c.geography_name or country}; not city-specific)" \
                if c.not_city_level else ""
            lines.append(f"- {c.statement}{geo}")
        body = (f"Source: {src.title or src.domain} ({src.url}).\n"
                f"Fact-checked statements relevant to {city}, {country}:\n" + "\n".join(lines))
        try:
            res = await d.graph.add_source_episode(city_slug=state["city_slug"], name=f"{src.domain}: {src.title}",
                                                   body=body, source_url=src.url, reference_time=_ref_time(src))
            async with db.session() as s:
                s.add(db.GraphEpisode(episode_uuid=res["episode_uuid"], run_id=run_id, source_id=sid,
                                      city_slug=state["city_slug"], claim_ids=[c.id for c in cs],
                                      n_nodes=res["n_nodes"], n_edges=res["n_edges"]))
                await s.commit()
            added += 1
            edges += res["n_edges"]
            await db.log_event(run_id, "build_graph", f"Graph: +{res['n_nodes']} entities, +{res['n_edges']} "
                                                      f"relations from {src.domain}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.exception("graph episode failed")
            await db.log_event(run_id, "build_graph", f"Graph episode failed for {src.domain}: {exc}"[:400], "warning")
    status = "ready" if added and not failed else ("partial" if added else "failed")
    await db.set_stage(run_id, "knowledge graph", graph_status=status)
    await db.log_event(run_id, "build_graph", f"Knowledge graph {status}: {added} source episodes, {edges} relations")
    return {}


async def synthesize_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id = state["run_id"]
    await db.set_stage(run_id, "synthesis")
    claims = await db.claims_for_run(run_id, ("supported", "partially_supported"))
    cl = [{"seq": c.seq, "dimension": c.dimension, "statement": c.statement, "value": c.value,
           "not_city_level": c.not_city_level, "geography_level": c.geography_level,
           "evidence_geography_level": c.geography_level, "verdict": c.verdict} for c in claims]
    brief = await synthesize(d.llm, state["city"], state.get("country", ""), cl, state.get("gaps", {}))
    await db.set_stage(run_id, "synthesis", brief=brief)
    await db.log_event(run_id, "synthesize", f"Briefing written; {len(brief.get('dropped', []))} generated "
                                             "statements dropped by the citation/number guard")
    return {}


async def finalize_node(state: ResearchState, config: RunnableConfig) -> ResearchState:
    d = _deps(config)
    run_id = state["run_id"]
    async with db.session() as s:
        sources = list((await s.execute(select(db.Source).where(db.Source.run_id == run_id))).scalars())
        claims = list((await s.execute(select(db.Claim).where(db.Claim.run_id == run_id))).scalars())
    stats = {
        "rounds": state.get("round", 1),
        "sources_considered": len(sources),
        "sources_blocked": sum(1 for x in sources if x.crawl_allowed is False),
        "sources_fetched": sum(1 for x in sources if x.status in ("fetched", "processed", "empty")),
        "claims_total": len(claims),
        "claims_supported": sum(1 for c in claims if c.verdict == "supported"),
        "claims_partial": sum(1 for c in claims if c.verdict == "partially_supported"),
        "claims_rejected": sum(1 for c in claims if c.verdict == "unsupported"),
        "claims_not_city_level": sum(1 for c in claims if c.not_city_level and c.verdict != "unsupported"),
        "llm_usage": getattr(d.llm, "usage", {}),
    }
    await db.set_stage(run_id, "done", status="done", stats=stats, finished_at=db.now())
    await db.log_event(run_id, "finalize", "Research complete")
    return {}


def build_workflow():
    g = StateGraph(ResearchState)
    g.add_node("plan", plan_node)
    g.add_node("search", search_node)
    g.add_node("crawl_gate", crawl_gate_node)
    g.add_node("fetch", fetch_node)
    g.add_node("extract", extract_node)
    g.add_node("fact_check", fact_check_node)
    g.add_node("assess_coverage", assess_coverage_node)
    g.add_node("followup_plan", followup_plan_node)
    g.add_node("resolve_conflicts", resolve_conflicts_node)
    g.add_node("build_graph", build_graph_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "search")
    g.add_edge("search", "crawl_gate")
    g.add_edge("crawl_gate", "fetch")
    g.add_edge("fetch", "extract")
    g.add_edge("extract", "fact_check")
    g.add_edge("fact_check", "assess_coverage")
    g.add_conditional_edges("assess_coverage", route_after_coverage,
                            {"followup_plan": "followup_plan", "resolve_conflicts": "resolve_conflicts"})
    g.add_edge("followup_plan", "search")
    g.add_edge("resolve_conflicts", "build_graph")
    g.add_edge("build_graph", "synthesize")
    g.add_edge("synthesize", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


WORKFLOW = build_workflow()


def workflow_mermaid() -> str:
    return WORKFLOW.get_graph().draw_mermaid()


async def execute_run(run_id: str, deps: Deps) -> None:
    async with db.session() as s:
        run = await s.get(db.Run, run_id)
        city, country = run.city, run.country
    try:
        await WORKFLOW.ainvoke({"run_id": run_id, "city": city, "country": country},
                               config={"configurable": {"deps": deps}, "recursion_limit": 60})
    except Exception as exc:  # noqa: BLE001
        log.exception("run %s failed", run_id)
        await db.set_stage(run_id, "failed", status="failed", error=f"{type(exc).__name__}: {exc}"[:2000],
                           finished_at=db.now())
        await db.log_event(run_id, "error", f"Run failed: {type(exc).__name__}: {exc}"[:500], "error")

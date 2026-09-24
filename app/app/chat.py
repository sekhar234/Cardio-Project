"""Conversational retrieval over a researched city.

Retrieval is hybrid and every hit carries an id the answer must cite:
  [C#]  verified claims       - semantic search in Qdrant, hydrated from Postgres (verdict, quote, source)
  [G#]  knowledge-graph facts - Graphiti hybrid search (BM25 + embeddings + graph) over the city's
                                 group, traced back to source documents via graph_episodes
  [P#]  raw source passages   - semantic search in Qdrant; shown as "not individually fact-checked"
The answer model may only use these; answers without a valid citation are replaced with an explicit
"not found", together with the relevant known gaps from the coverage assessment.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select

from . import db
from .graphstore import GraphProtocol
from .llm import LLMProtocol
from .vectorstore import VectorStore

SYSTEM = """You answer a City Lead's questions about a city using ONLY the evidence items provided.
Rules:
- Cite evidence ids inline, e.g. "... 32% of adults [C12]". Every factual sentence needs a citation.
- Prefer [C#] verified claims. [G#] graph facts are derived from verified claims. [P#] passages are raw
  source text that was not individually fact-checked - if you rely on one, say "according to <source>".
- If data is national or regional rather than city-level, say so explicitly.
- If the evidence does not answer the question, say so plainly and set not_found=true. Never guess,
  never use your own knowledge, never invent names, numbers, dates or attitudes."""


async def _rewrite(llm: LLMProtocol, question: str, history: list[dict[str, str]]) -> str:
    if not history:
        return question
    convo = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in history[-6:])
    data = await llm.json(task="rewrite", system="Rewrite follow-up questions as standalone questions.",
                          user=f"Conversation:\n{convo}\n\nFollow-up: {question}\n\n"
                               'Return JSON: {"question": "standalone question"}', max_tokens=300)
    return str(data.get("question") or question)


async def answer(*, run_id: str, question: str, llm: LLMProtocol, vectors: VectorStore,
                 graph: GraphProtocol) -> dict[str, Any]:
    async with db.session() as s:
        run = await s.get(db.Run, run_id)
        history = [{"role": m.role, "content": m.content} for m in (await s.execute(
            select(db.ChatMessage).where(db.ChatMessage.run_id == run_id).order_by(db.ChatMessage.id))).scalars()]
    standalone = await _rewrite(llm, question, history)
    qvec = (await llm.embed([standalone]))[0]

    claim_hits = await vectors.search(qvec, run.city_slug, "claim", limit=12, run_id=run_id)
    passage_hits = await vectors.search(qvec, run.city_slug, "passage", limit=5, run_id=run_id)
    graph_hits: list[dict[str, Any]] = []
    graph_error = ""
    if graph.enabled:
        try:
            graph_hits = await graph.search(standalone, run.city_slug, limit=10)
        except Exception as exc:  # noqa: BLE001
            graph_error = f"{type(exc).__name__}"

    evidence: dict[str, dict[str, Any]] = {}
    async with db.session() as s:
        claim_ids = [h["claim_id"] for h in claim_hits]
        claims = {c.id: c for c in (await s.execute(select(db.Claim).where(db.Claim.id.in_(claim_ids)))).scalars()}
        src_ids = {c.source_id for c in claims.values()} | {h["source_id"] for h in passage_hits}
        ep_ids = {e for g in graph_hits for e in g["episodes"]}
        episodes = {e.episode_uuid: e for e in (await s.execute(
            select(db.GraphEpisode).where(db.GraphEpisode.episode_uuid.in_(ep_ids)))).scalars()} if ep_ids else {}
        src_ids |= {e.source_id for e in episodes.values()}
        sources = {x.id: x for x in (await s.execute(select(db.Source).where(db.Source.id.in_(src_ids)))).scalars()}

    def src_info(sid: str) -> dict[str, Any]:
        x = sources.get(sid)
        if not x:
            return {}
        return {"source_id": x.id, "url": x.url, "title": x.title, "domain": x.domain,
                "credibility_tier": x.credibility_tier, "fetched_at": x.fetched_at.isoformat() if x.fetched_at else "",
                "published": x.published_date, "crawl_reason": x.crawl_reason}

    for cid in claim_ids:
        c = claims.get(cid)
        if not c or c.verdict not in ("supported", "partially_supported"):
            continue
        evidence[f"C{c.seq}"] = {"kind": "claim", "claim_id": c.id, "text": c.statement, "quote": c.quote,
                                 "verdict": c.verdict, "verdict_reason": c.verdict_reason,
                                 "not_city_level": c.not_city_level, "geography_level": c.geography_level,
                                 "year": c.year, "dimension": c.dimension, "source": src_info(c.source_id)}
    for i, g in enumerate(graph_hits, 1):
        srcs = [src_info(episodes[e].source_id) for e in g["episodes"] if e in episodes]
        if not srcs:
            continue  # a graph fact we cannot trace to a source is not shown
        evidence[f"G{i}"] = {"kind": "graph", "text": g["fact"], "relation": g["relation"],
                             "valid_at": g.get("valid_at"), "invalid_at": g.get("invalid_at"), "sources": srcs}
    for i, p in enumerate(passage_hits, 1):
        evidence[f"P{i}"] = {"kind": "passage", "text": p["text"][:900], "source": src_info(p["source_id"])}

    ctx = []
    for k, v in evidence.items():
        if v["kind"] == "claim":
            flag = " [NATIONAL/REGIONAL DATA, not city-specific]" if v["not_city_level"] else ""
            flag += " [partially supported]" if v["verdict"] == "partially_supported" else ""
            ctx.append(f"[{k}] VERIFIED CLAIM{flag}: {v['text']} (source: {v['source'].get('domain')})")
        elif v["kind"] == "graph":
            ctx.append(f"[{k}] GRAPH FACT: {v['text']} (from: {', '.join(s['domain'] for s in v['sources'])})")
        else:
            ctx.append(f"[{k}] UNVERIFIED PASSAGE from {v['source'].get('domain')}: {v['text']}")

    gaps = {k: [q["question"] for q in r["questions"] if q["status"] != "answered"]
            for k, r in (run.coverage or {}).items()}
    if not evidence:
        result = {"answer": "I could not find evidence about this in the research collected for "
                            f"{run.city}. It may be a gap worth raising directly with stakeholders.",
                  "citations": [], "not_found": True, "confidence": "none"}
    else:
        data = await llm.json(task="chat", system=SYSTEM, max_tokens=1800, user=(
            f"City: {run.city}, {run.country}\nQuestion: {standalone}\n\nEVIDENCE:\n" + "\n".join(ctx) +
            f"\n\nKnown research gaps: {json.dumps(gaps, ensure_ascii=False)[:1500]}\n\n"
            'Return JSON: {"answer": "markdown with inline [ids]", "not_found": true|false, '
            '"confidence": "high"|"medium"|"low", "missing": "what the evidence does not cover, or empty"}'))
        text = str(data.get("answer", "")).strip()
        cited = [c for c in dict.fromkeys(re.findall(r"\[([CGP]\d+)\]", text)) if c in evidence]
        # strip citations to ids we did not provide (a hallucinated citation is worse than none)
        text = re.sub(r"\[([CGP]\d+)\]", lambda m: m.group(0) if m.group(1) in evidence else "", text)
        not_found = bool(data.get("not_found"))
        if not cited and not not_found:
            text = ("I found related material but nothing that directly supports an answer, so I won't guess. "
                    "Try rephrasing, or treat this as an open question for the meeting.")
            not_found = True
        if data.get("missing"):
            text += f"\n\n_Not covered by the evidence:_ {data['missing']}"
        result = {"answer": text, "citations": cited, "not_found": not_found,
                  "confidence": data.get("confidence", "low")}

    result["evidence"] = {k: evidence[k] for k in result["citations"]}
    result["retrieval"] = {"standalone_question": standalone, "claims": len(claim_hits),
                           "graph_facts": len(graph_hits), "passages": len(passage_hits),
                           "graph_error": graph_error}
    async with db.session() as s:
        s.add(db.ChatMessage(run_id=run_id, role="user", content=question))
        s.add(db.ChatMessage(run_id=run_id, role="assistant", content=result["answer"],
                             citations=[{"id": k, **result["evidence"][k]} for k in result["citations"]]))
        await s.commit()
    return result


async def provenance(claim_id: str) -> dict[str, Any] | None:
    """The full evidence chain for one claim: query -> source -> crawl decision -> quote -> verdict -> graph."""
    async with db.session() as s:
        c = await s.get(db.Claim, claim_id)
        if not c:
            return None
        src = await s.get(db.Source, c.source_id)
        eps = [e for e in (await s.execute(select(db.GraphEpisode).where(
            db.GraphEpisode.source_id == c.source_id))).scalars() if c.id in (e.claim_ids or [])]
        conflict = await s.get(db.Conflict, c.conflict_group) if c.conflict_group else None
    return {
        "claim": {"id": c.id, "seq": c.seq, "statement": c.statement, "dimension": c.dimension,
                  "type": c.claim_type, "value": c.value, "unit": c.unit, "year": c.year,
                  "geography_level": c.geography_level, "geography_name": c.geography_name,
                  "not_city_level": c.not_city_level},
        "evidence": {"quote": c.quote, "quote_found_in_source": c.quote_found},
        "verification": {"verdict": c.verdict, "reason": c.verdict_reason, "checker": c.checker},
        "source": {"url": src.url, "final_url": src.final_url, "title": src.title, "domain": src.domain,
                   "credibility_tier": src.credibility_tier, "credibility_reason": src.credibility_reason,
                   "published": src.published_date,
                   "fetched_at": src.fetched_at.isoformat() if src.fetched_at else None,
                   "found_by_query": src.query},
        "crawl_permission": {"allowed": src.crawl_allowed, "reason": src.crawl_reason,
                             "checked_at": src.crawl_checked_at.isoformat() if src.crawl_checked_at else None},
        "graph": [{"episode_uuid": e.episode_uuid, "added_at": e.created_at.isoformat()} for e in eps],
        "conflict": {"description": conflict.description} if conflict else None,
    }

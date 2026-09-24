"""Offline end-to-end test: the full LangGraph workflow with deterministic fakes.

Checks the trust mechanics, not model quality:
  * blocked sources are never fetched
  * a fabricated quote and a wrong number are rejected before the LLM checker sees them
  * national data is flagged, even when the extractor labels it city-level
  * rejected claims never reach the graph, the vector index or the brief
  * the synthesis guard drops uncited bullets and bullets with numbers not in cited claims
  * coverage gaps trigger a follow-up research round
  * chat answers cite evidence, and say "not found" instead of guessing
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

os.environ["MAX_RESEARCH_ROUNDS"] = "2"

from app import db  # noqa: E402
from app.tools.crawl import CrawlabilityAgent, FetchResult  # noqa: E402
from app.vectorstore import VectorStore  # noqa: E402

DOC_OK = (
    "Testville Health Department annual report 2023.\n\n"
    "In 2022, the prevalence of hypertension among adults in Testville was 31.5% according to the city survey.\n\n"
    "Nationally, diabetes prevalence in Freedonia was 9.8% in 2021.\n\n"
    "The Healthy Hearts Testville programme was launched in 2019 by the Testville Health Department to "
    "screen adults for high blood pressure in primary care clinics.\n\n"
    "Dr. Ana Pereira, Secretary of Health of Testville, leads the city's NCD strategy.\n"
) * 2


class FakeLLM:
    def __init__(self):
        self.usage = {}
        self.calls: list[str] = []
        self.checker_saw: list[str] = []

    async def embed(self, texts):
        out = []
        for t in texts:
            h = hashlib.sha256(t.lower().encode()).digest()
            out.append([b / 255 for b in h[:8]])
        return out

    async def json(self, *, task, system, user, model=None, max_tokens=4000):
        self.calls.append(task)
        if task == "plan":
            return {"city": "Testville", "country": "Freedonia", "local_languages": ["en"], "queries": [
                {"dimension": d, "query": f"testville {d} q{i}", "language": "en"}
                for d in ["city_context", "cvd_burden", "risk_factors", "health_system", "programmes",
                          "policies", "stakeholders"] for i in range(2)]}
        if task == "followup_plan":
            return {"queries": [{"dimension": "health_system", "query": "testville primary care followup",
                                 "language": "en"}]}
        if task == "extract":
            if "Testville Health Department annual report" not in user:
                return {"claims": []}
            return {"claims": [
                {"dimension": "risk_factors", "claim_type": "statistic",
                 "statement": "In 2022, 31.5% of adults in Testville had hypertension.",
                 "quote": "In 2022, the prevalence of hypertension among adults in Testville was 31.5%",
                 "metric_key": "hypertension_prevalence", "value": 31.5, "unit": "%", "year": "2022",
                 "geography_level": "city", "geography_name": "Testville", "entities": []},
                # national data mislabelled as city -> checker must flag
                {"dimension": "risk_factors", "claim_type": "statistic",
                 "statement": "Diabetes prevalence in Testville was 9.8% in 2021.",
                 "quote": "Nationally, diabetes prevalence in Freedonia was 9.8% in 2021.",
                 "metric_key": "diabetes_prevalence", "value": 9.8, "unit": "%", "year": "2021",
                 "geography_level": "city", "geography_name": "Testville", "entities": []},
                # fabricated quote -> deterministic rejection
                {"dimension": "stakeholders", "claim_type": "person",
                 "statement": "John Smith is the Mayor of Testville.",
                 "quote": "John Smith, Mayor of Testville, opened the clinic",
                 "entities": [{"name": "John Smith", "type": "Person"}]},
                # wrong number -> deterministic rejection
                {"dimension": "programmes", "claim_type": "fact",
                 "statement": "Healthy Hearts Testville has screened 50000 adults since 2019.",
                 "quote": "The Healthy Hearts Testville programme was launched in 2019 by the Testville Health Department",
                 "entities": []},
                {"dimension": "programmes", "claim_type": "programme",
                 "statement": "The Healthy Hearts Testville programme, launched in 2019 by the Testville Health "
                              "Department, screens adults for high blood pressure in primary care clinics.",
                 "quote": "The Healthy Hearts Testville programme was launched in 2019 by the Testville Health "
                          "Department to screen adults for high blood pressure in primary care clinics.",
                 "entities": [{"name": "Healthy Hearts Testville", "type": "Programme"}]},
                {"dimension": "stakeholders", "claim_type": "person",
                 "statement": "Dr. Ana Pereira is Secretary of Health of Testville.",
                 "quote": "Dr. Ana Pereira, Secretary of Health of Testville, leads the city's NCD strategy.",
                 "entities": [{"name": "Ana Pereira", "type": "Person"}]},
            ]}
        if task == "fact_check":
            self.checker_saw.append(user)
            items = re.findall(r'"id": "([0-9a-f]+)"', user)
            res = []
            for i in items:
                block = user.split(f'"id": "{i}"', 1)[1][:600]
                national = "Diabetes" in block
                res.append({"id": i, "verdict": "supported",
                            "evidence_geography_level": "national" if national else "city",
                            "about_target_city": not national, "reason": "Stated in passage."})
            return {"results": res}
        if task == "coverage":
            return {"dimensions": []}  # everything missing -> forces follow-up round
        if task == "synthesize":
            return {
                "executive_summary": [
                    {"text": "Almost a third of adults in Testville (31.5%) have hypertension [C1].", "claims": ["C1"]},
                    {"text": "Invented: 75% of clinics are private.", "claims": ["C1"]},  # number not in C1
                    {"text": "Uncited statement.", "claims": []},
                ],
                "sections": {"programmes": {"points": [{"text": "Healthy Hearts Testville runs since 2019.",
                                                        "claims": ["C5"]}]}},
                "opportunities": [{"text": "Build on Healthy Hearts.", "claims": ["C5"],
                                   "assumption": "The programme is still active."}],
                "risks": [], "meeting_questions": ["What is hypertension control rate?"],
            }
        if task == "rewrite":
            return {"question": user.rsplit("Follow-up:", 1)[-1].split("\n")[0].strip()}
        if task == "chat":
            ids = re.findall(r"\[(C\d+)\] VERIFIED CLAIM", user)
            if "mayor" in user.lower().split("evidence:")[0]:
                return {"answer": "The evidence does not name the mayor.", "not_found": True, "confidence": "low"}
            return {"answer": f"Hypertension affects 31.5% of adults [{ids[0]}] [C999].", "not_found": False,
                    "confidence": "high"}
        raise AssertionError(task)


class FakeSearch:
    async def search(self, query, max_results=6):
        if "followup" in query:
            return [{"url": "https://health.testville.gov/primary-care", "title": "Primary care", "snippet": "",
                     "score": 0.5}]
        return [
            {"url": "https://health.testville.gov/report-2023", "title": "Annual report", "snippet": "", "score": 0.9},
            {"url": "https://www.linkedin.com/in/someone", "title": "Profile", "snippet": "", "score": 0.8},
            {"url": "https://blocked.example.org/page", "title": "Blocked", "snippet": "", "score": 0.7},
        ]


class FakeFetcher:
    def __init__(self):
        self.fetched: list[str] = []

    async def fetch(self, url):
        self.fetched.append(url)
        if "report-2023" in url:
            return FetchResult(True, final_url=url, status=200, content_type="text/html", title="Annual report",
                               published="2023-05-01", text=DOC_OK)
        return FetchResult(True, final_url=url, status=200, content_type="text/html", title="Other",
                           text="Unrelated page text. " * 30)


class FakeGraph:
    enabled = True

    def __init__(self):
        self.episodes = []

    async def init(self):
        pass

    async def add_source_episode(self, *, city_slug, name, body, source_url, reference_time):
        self.episodes.append(body)
        return {"episode_uuid": f"ep{len(self.episodes)}", "n_nodes": 3, "n_edges": 2}

    async def search(self, query, city_slug, limit=10):
        return [{"uuid": "e1", "fact": "Ana Pereira leads the NCD strategy of Testville", "relation": "LEADS",
                 "source_node_uuid": "a", "target_node_uuid": "b", "episodes": ["ep1"], "valid_at": None,
                 "invalid_at": None}]

    async def subgraph(self, city_slug, limit=400):
        return {"nodes": [], "edges": []}

    async def close(self):
        pass


def robots_transport():
    def handler(request: httpx.Request):
        if request.url.host == "blocked.example.org":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture()
def deps(tmp_path):
    from app.workflow import Deps

    db.configure(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    asyncio.get_event_loop_policy()
    crawler = CrawlabilityAgent(client=httpx.AsyncClient(transport=robots_transport()))
    return Deps(llm=FakeLLM(), search=FakeSearch(), crawler=crawler, fetcher=FakeFetcher(),
                vectors=VectorStore(client=AsyncQdrantClient(location=":memory:"), dim=8), graph=FakeGraph())


@pytest.mark.asyncio
async def test_full_workflow(deps):
    from app import chat, report
    from app.workflow import execute_run

    await db.init_db()
    async with db.session() as s:
        run = db.Run(city="Testville", country="Freedonia", city_slug="testville-freedonia")
        s.add(run)
        await s.commit()
        run_id = run.id

    await execute_run(run_id, deps)

    async with db.session() as s:
        run = await s.get(db.Run, run_id)
    assert run.status == "done", run.error
    assert run.stats["rounds"] == 2, "coverage gaps should trigger a follow-up round"

    sources = await db.sources_for_run(run_id)
    by_domain = {x.domain: x for x in sources}
    assert by_domain["linkedin.com"].crawl_allowed is False
    assert "Terms-of-use" in by_domain["linkedin.com"].crawl_reason
    assert by_domain["blocked.example.org"].crawl_allowed is False
    assert "disallowed" in by_domain["blocked.example.org"].crawl_reason
    assert not any("linkedin" in u or "blocked.example" in u for u in deps.fetcher.fetched), \
        "blocked sources must never be fetched"

    claims = await db.claims_for_run(run_id)
    by_stmt = {c.statement[:30]: c for c in claims}
    fabricated = next(c for c in claims if "John Smith" in c.statement)
    assert fabricated.verdict == "unsupported" and fabricated.checker == "deterministic"
    wrong_num = next(c for c in claims if "50000" in c.statement)
    assert wrong_num.verdict == "unsupported" and "50000" in wrong_num.verdict_reason
    diabetes = next(c for c in claims if "Diabetes" in c.statement)
    assert diabetes.not_city_level and diabetes.verdict == "partially_supported"
    assert diabetes.geography_level == "national"
    htn = next(c for c in claims if "31.5%" in c.statement)
    assert htn.verdict == "supported" and not htn.not_city_level
    # the independent checker never saw rejected claims
    assert not any("John Smith" in u for u in deps.llm.checker_saw)
    assert by_stmt  # silence

    # graph only receives verified claims
    graph_text = "\n".join(deps.graph.episodes)
    assert "John Smith" not in graph_text and "50000" not in graph_text
    # the graph holds relationships; statistics stay in Postgres/Qdrant
    assert "Ana Pereira" in graph_text and "31.5%" not in graph_text

    # synthesis guard
    brief = run.brief
    texts = [p["text"] for p in brief["executive_summary"]]
    assert any("31.5%" in t for t in texts)
    assert not any("75%" in t for t in texts) and not any("Uncited" in t for t in texts)
    assert len(brief["dropped"]) == 2
    assert brief["opportunities"][0]["assumption"]

    # coverage marks gaps
    assert run.coverage["health_system"]["status"] in ("thin", "missing")

    # chat: cites, strips hallucinated citation ids, traces graph facts to sources
    ans = await chat.answer(run_id=run_id, question="What share of adults have hypertension?", llm=deps.llm,
                            vectors=deps.vectors, graph=deps.graph)
    assert ans["citations"] and "[C999]" not in ans["answer"]
    assert all(ans["evidence"][c]["kind"] in ("claim", "graph", "passage") for c in ans["citations"])
    ans2 = await chat.answer(run_id=run_id, question="Who is the mayor?", llm=deps.llm, vectors=deps.vectors,
                             graph=deps.graph)
    assert ans2["not_found"]

    prov = await chat.provenance(htn.id)
    assert prov["crawl_permission"]["allowed"] and prov["evidence"]["quote_found_in_source"]
    assert prov["source"]["found_by_query"]

    md = report.to_markdown(report.build_blocks(await report.collect(run_id)))
    assert "31.5%" in md and "John Smith" not in md and "NATIONAL" in md
    assert len(report.to_docx(report.build_blocks(await report.collect(run_id)))) > 5000


def test_quote_locator_tolerates_pdf_noise():
    from app.tools.crawl import locate_quote

    src = "The preva-\nlence of hypertension   among adults was 31.5 % in 2022 in the\ncity of Testville."
    assert locate_quote("prevalence of hypertension among adults was 31.5 % in 2022 in the city", src.replace("-\n", ""))[0]
    assert not locate_quote("prevalence of diabetes among adults was 12 % in 2022 in the city", src)[0]


def test_workflow_graph_structure():
    from app.workflow import workflow_mermaid

    m = workflow_mermaid()
    for node in ("crawl_gate", "fact_check", "assess_coverage", "followup_plan", "build_graph"):
        assert node in m


@pytest.mark.asyncio
async def test_fetcher_streams_with_byte_cap_and_honours_opt_out():
    from app.tools.crawl import Fetcher

    big = b"%PDF-1.4 " + b"x" * 20_000_000
    html = ("<html><head><title>Health</title></head><body><article><p>"
            + "Hypertension screening in Testville reached many adults. " * 20 + "</p></article></body></html>")

    def handler(request: httpx.Request):
        if request.url.path == "/big.pdf":
            return httpx.Response(200, content=big, headers={"content-type": "application/pdf"})
        if request.url.path == "/noai":
            return httpx.Response(200, text=html, headers={"content-type": "text/html", "x-robots-tag": "noai"})
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    f = Fetcher()
    f.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await f.fetch("https://a.example.org/page")
    assert ok.ok and "Hypertension screening" in ok.text
    opt = await f.fetch("https://b.example.org/noai")
    assert not opt.ok and "opts out" in opt.error
    big_res = await f.fetch("https://c.example.org/big.pdf")  # must not load 20 MB, must not crash
    assert not big_res.ok

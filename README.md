# CARDIO4Cities City Intelligence

An agentic research assistant that prepares a CARDIO4Cities City Lead for a first meeting with a
city's government and health leaders. You give it a city name. It researches the public web live,
checks permission before crawling anything, verifies every claim with an independent fact checker,
builds a Graphiti knowledge graph, and produces a brief you can explore, question and download.
Every statement traces back to an exact quote in a source.

- **Architecture and design decisions:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **Example output:** [`samples/`](samples/)
- **Presentation deck:** [`docs/deck/`](docs/deck/)

## How it maps to the non-negotiables

| # | Requirement | Where |
|---|---|---|
| 1 | Live internet research | `app/tools/search.py` (Tavily at request time) + `app/tools/crawl.py` (own fetcher). No seeded data |
| 2 | Orchestrated agentic workflow | `app/workflow.py` (LangGraph `StateGraph`). Diagram in the app: *How it works* tab, or `GET /api/workflow` |
| 3 | Crawlability agent before crawling | `CrawlabilityAgent` in `app/tools/crawl.py`, node `crawl_gate`. Decisions shown in *Sources & permissions* |
| 4 | Independent fact checker with consequences | `app/agents/factchecker.py`, node `fact_check`. Rejected claims are excluded downstream; gaps trigger another research round |
| 5 | Graphiti knowledge graph used at query time | `app/graphstore.py`. Used in chat retrieval (`app/chat.py`) and the *Stakeholder graph* tab |
| 6 | Relational + vector + graph | PostgreSQL (`app/db.py`), Qdrant (`app/vectorstore.py`), Neo4j/Graphiti (`app/graphstore.py`) |
| 7 | Evidence on every fact | `GET /api/claims/{id}/provenance`; the *Where did this come from?* button on every citation |
| 8 | No fabrication; national ≠ city | Deterministic quote and number checks, geography re-check, synthesis citation/number guard |
| 9 | Deployed | Render (`render.yaml`) |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env        # fill in keys (OpenAI, Tavily, Neo4j Aura, Qdrant Cloud)
uvicorn app.main:app --reload
# open http://localhost:8000
```

With no `DATABASE_URL`, a local SQLite file is used. With no `QDRANT_URL`, an embedded Qdrant is
used under `./qdrant_local`. Neo4j is required for the graph features. Without it, the run still
completes and reports the graph as disabled.

### Tests

```bash
pytest -q
```

The end-to-end test runs the full LangGraph workflow offline with a deterministic fake LLM, search,
fetcher and graph. It checks the trust mechanics: blocked sources are never fetched, fabricated
quotes and altered numbers are rejected, national data is flagged, rejected claims never reach the
graph, brief or report, and chat refuses to answer without evidence.

## Deploy (Render)

1. Create a free **Neo4j Aura** instance and a free **Qdrant Cloud** cluster.
2. In Render: *New → Blueprint*, point it at this repository (`render.yaml` creates the web service
   and PostgreSQL).
3. Set the secret environment variables: `OPENAI_API_KEY`, `TAVILY_API_KEY`, `NEO4J_URI`,
   `NEO4J_USER`, `NEO4J_PASSWORD`, `QDRANT_URL`, `QDRANT_API_KEY`, and optionally `ACCESS_CODE`.
4. Open the service URL. The header shows the status of all three datastores.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RESEARCH_MODEL` | `gpt-5-mini` | Planner, extractor, coverage judge, synthesiser, chat |
| `CHECKER_MODEL` | `gpt-4.1` | Independent fact checker (deliberately a different model) |
| `GRAPH_MODEL` / `GRAPH_SMALL_MODEL` | `gpt-5-mini` / `gpt-4.1-nano` | Graphiti extraction |
| `MODEL_FALLBACKS` | `gpt-5-mini,gpt-4.1-mini,gpt-4o-mini` | Used if a model isn't available on the account |
| `MAX_QUERIES_PER_ROUND` | 16 | Research breadth |
| `MAX_SOURCES_PER_ROUND` | 18 | Pages fetched per round |
| `MAX_RESEARCH_ROUNDS` | 2 | Initial round + follow-ups on gaps |
| `ACCESS_CODE` | empty | If set, required to start runs and ask questions |

A run typically makes about 25–35 search calls, fetches 20–36 pages and costs roughly US$0.50–2 in
model usage. Most of that is Graphiti's graph extraction.

## Project layout

```
app/
  main.py            FastAPI API + static UI
  workflow.py        LangGraph workflow (nodes, routing)
  agents/            planner, extractor, fact checker, coverage judge, conflicts, synthesiser
  tools/             search (Tavily), crawlability + fetching + text extraction
  db.py              PostgreSQL models (system of record)
  vectorstore.py     Qdrant
  graphstore.py      Graphiti / Neo4j, entity ontology
  chat.py            hybrid retrieval + provenance
  report.py          Markdown / DOCX report
  static/            single-page UI (vanilla JS, vis-network, mermaid)
tests/               offline end-to-end workflow test
docs/                architecture, deck
samples/             example city report
```

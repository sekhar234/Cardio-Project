# Architecture & design decisions

## 1. The problem as I understand it

A City Lead has a meeting with government and health leaders in a city nobody on the team has
researched. They need a short, trustworthy picture of the city's cardiovascular landscape, and they
need to be able to say where every statement came from. Wrong or invented facts are worse than gaps:
repeating a made-up statistic or a wrong official's name in a government meeting damages credibility.

So the design goal is **trustworthy coverage**. That means breadth across what matters, verified
claims, gaps that are stated openly, and evidence you can trace. Volume is not the goal.

## 2. What "understanding a city" consists of

A fixed taxonomy of seven research dimensions (`app/taxonomy.py`), each with key questions and a
minimum amount of verified evidence:

| Dimension | Example key question | Min. verified claims |
|---|---|---|
| City context & governance | Which body runs health services; who leads it? | 2 |
| CVD burden | Share of deaths from CVD; stroke/IHD mortality | 3 |
| Risk factors | Hypertension prevalence, awareness, treatment, control; diabetes; lipids; lifestyle | 4 |
| Health system & primary care | Who provides primary care; workforce; coverage; digital systems | 3 |
| Existing programmes | Which hypertension/diabetes/NCD programmes run, by whom, since when | 3 |
| Policies | City plans; national/state NCD, tobacco, salt measures | 3 |
| Stakeholders | Named officials; hospitals, universities, NGOs, private actors | 3 |

Opportunities and risks are **analysis** derived from these facts. They are labelled that way and
must state their assumptions. Keeping the taxonomy explicit, rather than letting an LLM decide what
matters, makes coverage measurable and gaps nameable.

## 3. Agent architecture (LangGraph)

```
plan → search → crawl_gate → fetch → extract → fact_check → assess_coverage ─┬─> resolve_conflicts → build_graph → synthesize → finalize
          ^                                                                   │
          └──────────────────── followup_plan <──── (gaps and budget left) ───┘
```

| Agent / node | Responsibility | Can it create facts? |
|---|---|---|
| Planner | Resolve/disambiguate the city, pick local languages, write 2 queries per dimension | No (queries only) |
| Search | Tavily search at request time. Results are **leads**; snippets are never evidence | No |
| **Crawlability agent** | Terms-of-use policy + robots.txt per URL **before any page request**. Also honours `X-Robots-Tag`/meta `noai` after fetch, and re-checks when a redirect changes domain | No |
| Fetcher | Polite fetch (1 req/s/host, own user-agent), HTML via trafilatura, PDF via pypdf | No |
| Extractor | Proposes atomic claims per source, each with a **verbatim quote**, year, geography level, metric key and entities | Proposes only (`pending`) |
| **Fact checker** (independent) | Layer 1, deterministic: the quote must exist in the fetched text and every number must exist in the passage. Layer 2: a **different model** with its own prompt sees only the claim and the raw source passage, and judges support, year and geography | Can only downgrade |
| Coverage judge | Counts verified claims per dimension, maps them to key questions, names unanswered ones | No |
| Follow-up planner | Targeted queries for the gaps (different angles, local language, survey names) | No |
| Conflict resolver | Deterministic grouping by metric/geography/unit/year | No |
| Graph builder | Writes verified claims to Graphiti, one episode per source | No (graph derived from verified claims) |
| Synthesiser | Writes the brief from verified claims only; a guard removes uncited bullets and numbers not in the cited claims | Wording only |

**Consequences of fact checking in the workflow:**
- Unsupported claims are excluded from the graph, the vector index, the brief, the report and chat.
  They stay in Postgres as an audit trail and can be viewed under Evidence → "Rejected only".
- Geography corrections set `not_city_level`, which is shown everywhere ("national data, not
  city-specific") and never counts as city-level coverage.
- Coverage is computed from verified claims only. A dimension that was researched but produced
  only rejected claims is still a gap, which triggers another research round. If the round budget
  is spent, it becomes a named unknown.

**Why a different model for the checker?** Errors from the same model and prompt are correlated.
Using a different model family, a fresh context and no access to the extractor's reasoning makes
it more likely that errors get caught. The deterministic layer catches the most damaging failures
(invented quotes, altered numbers) without relying on any model.

## 4. Data architecture: what lives where

| Store | Holds | Why there |
|---|---|---|
| **PostgreSQL** (relational) | runs, run events, sources, crawl decisions, claims (+ quote, verdict, reason, geography), conflicts, graph-episode↔source map, chat log | System of record and audit trail. "Where did this come from?" is a join: claim → source → crawl decision → search query. Transactional, queryable, and it outlives any index |
| **Qdrant** (vector) | embeddings of verified claims and of source passages, with ids and short text only | Semantic retrieval for chat. It is disposable and can be rebuilt from Postgres, so it never holds anything that is only stored there |
| **Neo4j + Graphiti** (graph) | Person, Organization, Programme, Policy, Place and HealthCondition entities with time-stamped fact edges | Relationship questions ("who runs which programme", "which bodies are linked to the NCD strategy") and institutional memory: entities are deduplicated across sources and runs, and Graphiti's bi-temporal edges let newer facts supersede older ones |

**How Graphiti is used at query time:**
1. Chat: `graphiti.search()` (hybrid BM25 + embeddings + graph) over the city's `group_id`. Each
   returned edge carries its episode ids. These map through `graph_episodes` to source documents,
   so graph facts are cited with sources like everything else. A graph fact that can't be traced
   is not shown.
2. The Stakeholder graph tab reads entities and edges for the city and shows the facts and sources
   behind each node.

## 5. Retrieval strategy (chat)

1. A follow-up question is rewritten into a standalone question using the chat history.
2. Parallel retrieval: verified claims (Qdrant, filtered to supported/partial), graph facts
   (Graphiti), and raw passages (Qdrant).
3. The answer model receives only these items with ids `[C#]`, `[G#]` and `[P#]`, plus the known
   gaps. Passages are labelled "not individually fact-checked".
4. Post-validation: citations to ids that weren't supplied are removed. If no valid citation
   remains, the answer is replaced with an explicit "not found".
5. Each citation opens the provenance chain.

## 6. Answers to the open design questions

- **How should research be planned?** Around the taxonomy, with two queries per dimension,
  interleaved so budget cuts don't starve any dimension. Local-language queries are included
  because city-level sources are often published only in the local language.
- **How should sources be evaluated?** A credibility tier by publisher type (official or
  peer-reviewed / established org or media / other) shapes which sources are selected and is shown
  to the user. It does not affect verification. A tier-1 source can still be misread, so every claim
  is checked the same way.
- **Should humans review before storing?** Not as a gate. At demo scale it would block the
  workflow. The design makes human review easy afterwards: every claim shows its verdict, reason
  and quote, and rejected claims stay visible. A production version would add "confirm / dispute"
  on claims, written back to Postgres and used to invalidate graph edges.
- **When is research sufficient?** When each dimension meets its minimum verified claims and at
  most half its key questions are unanswered. Otherwise the system runs another targeted round,
  up to `MAX_RESEARCH_ROUNDS`, and the remaining gaps are published as unknowns.
- **Conflicting information?** Resolved deterministically. Same metric, geography, unit and period
  with >10% difference → both values are shown side by side, and neither is picked without a
  methodological reason. Different periods → time series, with the latest treated as current.
- **Institutional memory over time?** Postgres keeps every run immutably. Graphiti partitions the
  graph by city (`group_id`), so a later run on the same city adds episodes to the existing graph.
  Entities are merged, and Graphiti's temporal model marks superseded facts (`invalid_at`), shown
  as dashed edges.
- **Modelling people, organisations, programmes, policies?** Six typed entities in Graphiti with a
  few optional attributes (role title, org kind, focus area, jurisdiction, year). People only when
  the source names them and states their role. The extraction instructions forbid inferring
  affiliations or attitudes.
- **Facts vs assumptions?** Facts are verified claims with quotes. Analysis (opportunities, risks)
  is labelled and carries an explicit "Assumes: …". Planner output such as the city
  disambiguation is shown as an assumption, not a fact.
- **Missing information?** A first-class object: per dimension and per key question the status
  is answered / partial / not found. It is shown in the brief, the Gaps tab, the report, and
  turned into "questions to raise in the meeting".
- **National vs city data?** The extractor labels geography. The checker independently
  re-derives it from the passage. Any non-city claim is flagged and cannot count as city coverage.

## 7. Trade-offs and what I cut (2–3 day timebox)

| Decision | Why | Cost |
|---|---|---|
| Rule-based crawlability (robots.txt + ToS list) rather than an LLM reading each ToS page | Deterministic, explainable, fast. Reading ToS pages would itself require crawling them | The ToS list is curated, not exhaustive |
| Only fact-checked claims go to the graph | Keeps the graph trustworthy | Less recall; some true facts are dropped when the quote is noisy (e.g. PDF tables) |
| One episode per source, sequential | Better entity resolution and provenance | Graph build is the slowest stage (~20–40 s per source) |
| Background task in the web process, not a job queue | Simple to deploy and demo | A restart interrupts a running job (it is marked "interrupted", never left hanging) |
| No login; optional shared `ACCESS_CODE` | Enough to stop strangers spending the API budget | No per-user audit |
| No JS-rendered pages (no headless browser) | Memory and time on a small instance | Some modern government sites give little text |
| Report is DOCX/Markdown, not PDF | No heavy system dependencies; DOCX is editable by City Leads | Users convert to PDF themselves |

**Not built:** human review workflow, scheduled refresh of stale facts, source-quality scoring
beyond publisher type, multi-city comparison, evaluation harness against a gold-standard city.
Those would be my next steps, in that order.

## 8. Known limitations

- Recall depends on the web search results and on what permits crawling. Many city health
  statistics exist only in PDFs or on JS-heavy pages.
- The fact checker is strict about numbers. Correct claims with reformatted numbers (e.g. "one in
  three" vs 33%) are rejected. This is deliberate: over-rejecting is safer than over-accepting.
- LLM judgements (checker, coverage) are not perfectly consistent between runs.
- Credibility tiers are heuristic and domain-based.

"""Research planner: turns a city name into a structured research plan.

The plan is anchored to the fixed taxonomy (app/taxonomy.py). The LLM contributes what it is
good at - disambiguating the city, knowing its languages, and phrasing effective search queries -
but it never contributes facts: anything it "knows" about the city is treated as a lead, not evidence.
"""
from __future__ import annotations

import json
from typing import Any

from ..llm import LLMProtocol
from ..taxonomy import DIMENSION_BY_KEY, DIMENSIONS

SYSTEM = """You plan desk research for a global cardiovascular health programme (CARDIO4Cities) that is
preparing to meet government and healthcare leaders in a city. You write web search queries only; you do
not state facts about the city. Queries should find primary, citable sources: government and municipal
health department pages, official statistics, WHO/STEPS surveys, peer-reviewed studies, programme pages,
policy documents and reputable news about named officials."""


def plan_prompt(city: str, country: str, per_dimension: int) -> str:
    dims = "\n".join(
        f"- {d.key}: {d.title}. Key questions: " + " | ".join(d.key_questions) for d in DIMENSIONS
    )
    return f"""City requested: "{city}"{f', country: "{country}"' if country else ''}.

Research dimensions:
{dims}

Return JSON:
{{
  "city": "canonical city name",
  "country": "country",
  "admin_region": "state/province or empty",
  "local_languages": ["languages used in official documents, most important first"],
  "ambiguity_note": "if the name could refer to several places, say which one you chose and why; else empty",
  "queries": [{{"dimension": "<dimension key>", "query": "<search query>", "language": "<ISO code>"}}]
}}

Rules:
- {per_dimension} queries per dimension. Always include the city name (and the country if the name is ambiguous).
- Where the official language is not English, write about a third of queries in that language
  (e.g. "prevalência de hipertensão São Paulo" or "उच्च रक्तचाप" terms) - local sources are often the only city-level sources.
- For risk_factors and cvd_burden, include at least one query aimed at city-level survey data and one aimed at
  national survey data (clearly national).
- For stakeholders, target the city health department leadership and institutions, not individuals' social media.
- No site: operators, no quotation marks."""


def followup_prompt(city: str, country: str, gaps: dict[str, list[str]], tried: list[str], n: int) -> str:
    return f"""City: {city}, {country}.
Earlier searches did not answer these questions (by dimension):
{json.dumps(gaps, indent=1, ensure_ascii=False)}

Queries already tried (do not repeat them):
{json.dumps(tried[-40:], ensure_ascii=False)}

Write at most {n} new, differently-angled search queries targeting the unanswered questions (try official
report titles, survey names such as STEPS or national health surveys, local-language terms, or the names of
institutions likely to publish the data).
Return JSON: {{"queries": [{{"dimension": "<key>", "query": "...", "language": "<ISO>"}}]}}"""


def _clean_queries(raw: list[dict[str, Any]], limit: int) -> list[dict[str, str]]:
    seen, out = set(), []
    for q in raw or []:
        text = str(q.get("query", "")).strip()
        dim = q.get("dimension", "")
        if not text or dim not in DIMENSION_BY_KEY or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append({"dimension": dim, "query": text[:300], "language": str(q.get("language", "en"))[:8]})
    # round-robin across dimensions so a budget cut does not starve any one dimension
    by_dim: dict[str, list[dict[str, str]]] = {}
    for q in out:
        by_dim.setdefault(q["dimension"], []).append(q)
    interleaved: list[dict[str, str]] = []
    while any(by_dim.values()) and len(interleaved) < limit:
        for d in list(by_dim):
            if by_dim[d] and len(interleaved) < limit:
                interleaved.append(by_dim[d].pop(0))
    return interleaved


async def make_plan(llm: LLMProtocol, city: str, country: str, max_queries: int) -> dict[str, Any]:
    per_dim = max(1, min(3, max_queries // len(DIMENSIONS)))
    data = await llm.json(task="plan", system=SYSTEM, user=plan_prompt(city, country, per_dim), max_tokens=2500)
    data["queries"] = _clean_queries(data.get("queries", []), max_queries)
    data.setdefault("city", city)
    data.setdefault("country", country)
    return data


async def make_followups(llm: LLMProtocol, city: str, country: str, gaps: dict[str, list[str]],
                         tried: list[str], max_queries: int) -> list[dict[str, str]]:
    data = await llm.json(task="followup_plan", system=SYSTEM,
                          user=followup_prompt(city, country, gaps, tried, max_queries), max_tokens=1500)
    tried_l = {t.lower() for t in tried}
    return [q for q in _clean_queries(data.get("queries", []), max_queries) if q["query"].lower() not in tried_l]

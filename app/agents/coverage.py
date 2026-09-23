"""Coverage judge: decides whether research is sufficient, and names what is missing.

Sufficiency is measured, not guessed:
  * per dimension, count *verified* claims against a minimum (taxonomy.min_supported_claims);
  * an LLM maps verified claims to each dimension's key questions and lists unanswered ones.
If any dimension is thin and budget remains, the workflow loops back to research with targeted
follow-up queries. When budget runs out, the unanswered questions become explicit, named gaps
("not found in N sources checked") in the UI and report - never silently filled.
"""
from __future__ import annotations

import json
from typing import Any

from ..llm import LLMProtocol
from ..taxonomy import DIMENSIONS

# For these dimensions national data alone does not describe the city.
CITY_LEVEL_REQUIRED = {"cvd_burden", "risk_factors", "stakeholders"}

SYSTEM = """You audit research coverage. You decide which research questions are answered by a set of
verified claims. A question is answered only if a claim directly answers it; related context is not enough.
Data about the country (not the city) only partially answers a city question - mark it 'partial'."""


def prompt(city: str, claims_by_dim: dict[str, list[str]]) -> str:
    dims = []
    for d in DIMENSIONS:
        dims.append({
            "dimension": d.key,
            "questions": list(d.key_questions),
            "verified_claims": claims_by_dim.get(d.key, [])[:25],
        })
    return f"""City: {city}
{json.dumps(dims, ensure_ascii=False, indent=1)}

Return JSON: {{"dimensions": [{{"dimension": "<key>", "questions": [
   {{"question": "<exact question text>", "status": "answered" | "partial" | "missing"}}]}}]}}"""


async def assess(llm: LLMProtocol, city: str, claims: list[dict[str, Any]]) -> dict[str, Any]:
    by_dim: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    city_level: dict[str, int] = {}
    for c in claims:
        tag = " [NATIONAL/REGIONAL DATA]" if c.get("not_city_level") else ""
        by_dim.setdefault(c["dimension"], []).append(c["statement"][:300] + tag)
        counts[c["dimension"]] = counts.get(c["dimension"], 0) + 1
        if not c.get("not_city_level"):
            city_level[c["dimension"]] = city_level.get(c["dimension"], 0) + 1

    data = await llm.json(task="coverage", system=SYSTEM, user=prompt(city, by_dim), max_tokens=3000)
    judged = {d.get("dimension"): d.get("questions", []) for d in data.get("dimensions", []) if isinstance(d, dict)}

    report: dict[str, Any] = {}
    for d in DIMENSIONS:
        qs = []
        for q in d.key_questions:
            status = "missing"
            for jq in judged.get(d.key, []):
                if isinstance(jq, dict) and str(jq.get("question", "")).strip()[:60] == q[:60]:
                    status = jq.get("status", "missing")
            qs.append({"question": q, "status": status if status in ("answered", "partial", "missing") else "missing"})
        n = counts.get(d.key, 0)
        missing = [q["question"] for q in qs if q["status"] == "missing"]
        city_n = city_level.get(d.key, 0)
        if n == 0:
            status = "missing"
        elif d.key in CITY_LEVEL_REQUIRED and city_n == 0:
            status = "thin"  # only national/regional data: useful context, but not the city's picture
        elif n >= d.min_supported_claims and len(missing) <= len(qs) // 2:
            status = "sufficient"
        else:
            status = "thin"
        report[d.key] = {
            "title": d.title, "verified_claims": n, "city_level_claims": city_level.get(d.key, 0),
            "min_required": d.min_supported_claims, "status": status, "questions": qs,
        }
    return report


def gaps_from(report: dict[str, Any]) -> dict[str, list[str]]:
    """Questions still missing or only partially answered, for follow-up research."""
    out: dict[str, list[str]] = {}
    for key, r in report.items():
        if r["status"] == "sufficient":
            continue
        qs = [q["question"] for q in r["questions"] if q["status"] != "answered"]
        if qs:
            out[key] = qs
    return out

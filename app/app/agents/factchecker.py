"""Independent fact-checking agent.

Independence, concretely:
  * separate model (settings.checker_model) and separate prompt from the extractor;
  * it never sees the extractor's output reasoning - only the claim as stated and the raw source
    passage located by a deterministic quote search;
  * it can only down-grade: it may reject or qualify a claim, never add new facts.

Two layers:
  1. Deterministic gate (no LLM): the quote must actually occur in the fetched text, and every number
     in the claim must occur in the surrounding passage. Failing claims are rejected as
     'unsupported' immediately - this is what catches fabricated quotes and invented statistics.
  2. LLM judgement on the survivors: does the passage support the statement, including year and
     geography? Is the data really about the city, or national/regional?

Consequences in the workflow: unsupported claims are excluded from the graph, the vector index,
the report and chat answers; they remain visible in the audit trail. Geography corrections set
`not_city_level`, which the UI and report surface as "national data - not city-specific".
"""
from __future__ import annotations

import json
from typing import Any

from ..config import settings
from ..llm import LLMProtocol
from ..taxonomy import CITY_LEVELS, GEOGRAPHY_LEVELS
from ..tools.crawl import locate_quote, normalise, numbers_in, passage_around

SYSTEM = """You are an independent fact checker. You did not write the claims you are given and you
assume nothing about them is correct. You judge each claim ONLY against the source passage provided -
not against your own knowledge, even if you believe the claim is true. You are strict about numbers,
years, units and geography: data about a country or state is not data about a city."""


def prompt(city: str, country: str, items: list[dict[str, Any]]) -> str:
    return f"""Target city: {city} ({country}).

For each item, compare the CLAIM with its SOURCE PASSAGE.
{json.dumps(items, ensure_ascii=False, indent=1)}

Return JSON: {{"results": [{{
  "id": "<item id>",
  "verdict": "supported" | "partially_supported" | "unsupported",
  "evidence_geography_level": one of {list(GEOGRAPHY_LEVELS)} (the area the passage's data actually describes),
  "about_target_city": true | false (false if the passage is about a different place, or only about the country),
  "reason": "one sentence explaining the verdict, naming any mismatch"
}}]}}

Definitions:
- supported: every element of the claim (numbers, units, year, geography, names, roles) is stated in the passage.
- partially_supported: the core fact is stated, but a qualifier in the claim (year, geography, role, scope) is
  missing from or different in the passage.
- unsupported: the passage does not state the claim, contradicts it, or is about something else."""


def _number_check(claim: dict[str, Any], passage: str) -> str:
    """Return a reason string if a number in the claim is missing from the passage."""
    pnums = numbers_in(passage)
    pvals = set()
    for n in pnums:
        try:
            pvals.add(round(float(n), 4))
        except ValueError:
            pass
    wanted = set(numbers_in(claim["statement"]))
    if claim.get("value") is not None:
        v = float(claim["value"])
        wanted.add(str(int(v)) if v.is_integer() else str(v))
    missing = []
    for n in wanted:
        try:
            if round(float(n), 4) not in pvals:
                missing.append(n)
        except ValueError:
            continue
    # years in the statement are commonly phrased differently (e.g. "2019-20"); tolerate 4-digit years
    missing = [m for m in missing if not (len(m) == 4 and m.isdigit() and m[:2] in ("19", "20"))]
    return f"number(s) {', '.join(sorted(missing))} not found in the source passage" if missing else ""


async def check_source_claims(llm: LLMProtocol, *, city: str, country: str, source_text: str,
                              claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """claims: [{id, statement, quote, value, year, geography_level, ...}] -> {id: verdict dict}"""
    results: dict[str, dict[str, Any]] = {}
    to_llm: list[dict[str, Any]] = []
    for c in claims:
        found, _ = locate_quote(c["quote"], source_text)
        if not found:
            results[c["id"]] = {"verdict": "unsupported", "quote_found": False, "checker": "deterministic",
                                "reason": "Quoted text was not found in the fetched source (possible fabrication).",
                                "not_city_level": c.get("geography_level") not in CITY_LEVELS}
            continue
        passage = passage_around(source_text, c["quote"], window=900)
        bad_numbers = _number_check(c, passage)
        if bad_numbers:
            results[c["id"]] = {"verdict": "unsupported", "quote_found": True, "checker": "deterministic",
                                "reason": f"Claim cites {bad_numbers}.",
                                "not_city_level": c.get("geography_level") not in CITY_LEVELS}
            continue
        to_llm.append({"id": c["id"], "claim": c["statement"],
                       "claimed_geography": f"{c.get('geography_level')}: {c.get('geography_name', '')}",
                       "claimed_year": c.get("year", ""), "source_passage": passage[:2400]})

    for i in range(0, len(to_llm), 8):
        batch = to_llm[i:i + 8]
        data = await llm.json(task="fact_check", system=SYSTEM, user=prompt(city, country, batch),
                              model=settings.checker_model, max_tokens=2500)
        by_id = {str(r.get("id")): r for r in data.get("results", []) if isinstance(r, dict)}
        for item in batch:
            r = by_id.get(item["id"])
            claim = next(c for c in claims if c["id"] == item["id"])
            if r is None:
                results[item["id"]] = {"verdict": "unsupported", "quote_found": True, "checker": "llm",
                                       "reason": "Fact checker returned no verdict; excluded by default.",
                                       "not_city_level": claim.get("geography_level") not in CITY_LEVELS}
                continue
            verdict = r.get("verdict") if r.get("verdict") in ("supported", "partially_supported", "unsupported") \
                else "unsupported"
            geo = r.get("evidence_geography_level") if r.get("evidence_geography_level") in GEOGRAPHY_LEVELS \
                else claim.get("geography_level", "unknown")
            not_city = geo not in CITY_LEVELS or r.get("about_target_city") is False
            reason = str(r.get("reason", ""))[:500]
            if not_city and claim.get("geography_level") in CITY_LEVELS:
                # extractor presented broader data as city data -> qualify it and say so
                reason = (reason + " Geography corrected: evidence describes " + geo + " data, not the city.").strip()
                if verdict == "supported":
                    verdict = "partially_supported"
            results[item["id"]] = {"verdict": verdict, "quote_found": True, "checker": "llm", "reason": reason,
                                   "not_city_level": not_city, "evidence_geography_level": geo}
    return results


def claim_key(claim_id: str) -> str:
    return f"claim:{claim_id}"


def normalised_statement(s: str) -> str:
    return normalise(s)

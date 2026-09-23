"""Extraction agent: reads one fetched source and proposes atomic claims, each with a verbatim quote.

It proposes; it does not decide what is true. Everything it produces is 'pending' until the
independent fact checker has ruled on it.
"""
from __future__ import annotations

from typing import Any

from ..llm import LLMProtocol
from ..taxonomy import DIMENSION_KEYS, GEOGRAPHY_LEVELS, METRIC_KEYS

SYSTEM = """You extract evidence for a cardiovascular health programme preparing to work with a city.
You only record what the provided document explicitly says. You never add knowledge from memory,
never estimate, never infer people's roles or attitudes, and never round or convert numbers."""

CLAIM_TYPES = ("statistic", "programme", "policy", "organisation", "person", "fact")


def prompt(city: str, country: str, title: str, url: str, text: str, max_claims: int) -> str:
    return f"""Target city: {city} ({country}).
Document title: {title or '(none)'}
Document URL: {url}

DOCUMENT TEXT (may be an excerpt):
<<<
{text}
>>>

Extract up to {max_claims} claims that help someone understand {city}'s cardiovascular health landscape:
disease burden and risk factors (hypertension, diabetes, dyslipidaemia, obesity, tobacco, salt, inactivity),
the health system and primary care, existing programmes, policies, and named stakeholders/organisations.
National or state data about {country} is useful but MUST be labelled with its true geography.

Return JSON: {{"claims": [{{
  "dimension": one of {list(DIMENSION_KEYS)},
  "claim_type": one of {list(CLAIM_TYPES)},
  "statement": "one self-contained sentence; include the year and the geography the data refers to",
  "quote": "an exact, contiguous, copy-pasted span from the document text (<= 350 characters) that proves the statement",
  "metric_key": one of {list(METRIC_KEYS)} for statistics, else "",
  "value": number or null (only for statistics, exactly as in the text),
  "unit": "%, per 100,000, people, etc. or empty",
  "year": "year or period the data refers to, or empty if not stated",
  "geography_level": one of {list(GEOGRAPHY_LEVELS)} - the area the DATA describes (not where the publisher is),
  "geography_name": "name of that area",
  "entities": [{{"name": "...", "type": "Person|Organization|Programme|Policy|Place|HealthCondition"}}]
}}]}}

Hard rules:
- The quote must be copied character-for-character from the document text. If you cannot quote it, do not claim it.
- Every number in the statement must appear in the quote.
- A person may only be included if the document names them AND states their role; copy the role verbatim.
- Do not record opinions or attitudes unless they are an explicitly attributed quotation.
- If the document is not about {city} or {country}, or has nothing relevant, return {{"claims": []}}.
- If the document is about a different place with the same name, return {{"claims": []}}."""


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


async def extract_claims(llm: LLMProtocol, *, city: str, country: str, title: str, url: str, text: str,
                         max_claims: int = 12) -> list[dict[str, Any]]:
    data = await llm.json(task="extract", system=SYSTEM,
                          user=prompt(city, country, title, url, text, max_claims), max_tokens=5000)
    out = []
    for c in (data.get("claims") or [])[:max_claims]:
        statement = str(c.get("statement", "")).strip()
        quote = str(c.get("quote", "")).strip()
        if not statement or not quote:
            continue
        dim = c.get("dimension") if c.get("dimension") in DIMENSION_KEYS else "city_context"
        ctype = c.get("claim_type") if c.get("claim_type") in CLAIM_TYPES else "fact"
        geo = c.get("geography_level") if c.get("geography_level") in GEOGRAPHY_LEVELS else "unknown"
        metric = c.get("metric_key") if c.get("metric_key") in METRIC_KEYS else ""
        ents = [
            {"name": str(e.get("name", ""))[:200], "type": str(e.get("type", ""))[:40]}
            for e in (c.get("entities") or []) if isinstance(e, dict) and e.get("name")
        ]
        out.append({
            "dimension": dim, "claim_type": ctype, "statement": statement[:1000], "quote": quote[:1200],
            "metric_key": metric if ctype == "statistic" else "", "value": _as_float(c.get("value")),
            "unit": str(c.get("unit") or "")[:60], "year": str(c.get("year") or "")[:16],
            "geography_level": geo, "geography_name": str(c.get("geography_name") or "")[:200], "entities": ents,
        })
    return out

"""Synthesis agent: writes the City Lead briefing from verified claims only.

Guards against fabrication at the output end:
  * the model sees only verified claims (with ids) - no source text, no web access;
  * every bullet must cite claim ids; bullets citing nothing valid are dropped;
  * every number in a bullet must appear in one of the claims it cites, or the bullet is dropped;
  * opportunities and risks are labelled 'analysis' and must state their assumption explicitly.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLMProtocol
from ..taxonomy import DIMENSIONS
from ..tools.crawl import numbers_in

SYSTEM = """You write a concise pre-meeting briefing for a CARDIO4Cities City Lead. You may use ONLY the
verified claims provided. Do not add facts, numbers, names or dates from your own knowledge. Every bullet
cites the claim ids it relies on. If a claim is marked NATIONAL or REGIONAL, say so in the sentence
("nationally, ..."). Distinguish clearly between what the evidence says and what you infer."""


def prompt(city: str, country: str, claims: list[dict[str, Any]], gaps: dict[str, Any]) -> str:
    lines = []
    for c in claims:
        flags = []
        if c.get("not_city_level"):
            flags.append(f"{(c.get('evidence_geography_level') or c.get('geography_level') or 'non-city').upper()} DATA")
        if c.get("verdict") == "partially_supported":
            flags.append("PARTIALLY SUPPORTED")
        lines.append(f"C{c['seq']} [{c['dimension']}]{' [' + '; '.join(flags) + ']' if flags else ''}: {c['statement']}")
    dims = ", ".join(d.key for d in DIMENSIONS)
    return f"""City: {city} ({country})

VERIFIED CLAIMS:
{chr(10).join(lines)}

KNOWN GAPS (questions research could not answer):
{json.dumps(gaps, ensure_ascii=False)}

Return JSON:
{{
  "executive_summary": [{{"text": "...", "claims": ["C1", "C7"]}}],          // 3-5 bullets
  "sections": {{"<dimension key>": {{"points": [{{"text": "...", "claims": ["C3"]}}]}}}},  // keys from: {dims}
  "opportunities": [{{"text": "...", "claims": ["C2"], "assumption": "what must be true for this to hold"}}],
  "risks": [{{"text": "...", "claims": ["C5"], "assumption": "..."}}],
  "meeting_questions": ["questions the City Lead should ask stakeholders to close the known gaps"]
}}
Up to 5 points per section. Omit a section if no claims support it (the gap will be shown instead)."""


_CITE = re.compile(r"^C?(\d+)$")


def _validate_points(points: Any, by_seq: dict[int, dict[str, Any]], dropped: list[str],
                     analysis: bool = False) -> list[dict[str, Any]]:
    out = []
    for p in points or []:
        if not isinstance(p, dict):
            continue
        text = str(p.get("text", "")).strip()
        ids = []
        for raw in p.get("claims") or []:
            m = _CITE.match(str(raw).strip())
            if m and int(m.group(1)) in by_seq:
                ids.append(int(m.group(1)))
        if not text or not ids:
            dropped.append(f"No valid citation: {text[:120]}")
            continue
        cited_numbers: set[float] = set()
        for i in ids:
            src = by_seq[i]
            for n in numbers_in(src["statement"]) | ({str(src["value"])} if src.get("value") is not None else set()):
                try:
                    cited_numbers.add(round(float(n), 4))
                except ValueError:
                    pass
        text_wo_cites = re.sub(r"\[?C\d+\]?", "", text)
        bad = []
        for n in numbers_in(text_wo_cites):
            try:
                if round(float(n), 4) not in cited_numbers:
                    bad.append(n)
            except ValueError:
                continue
        if bad:
            dropped.append(f"Number(s) {bad} not in cited claims: {text[:120]}")
            continue
        item: dict[str, Any] = {"text": re.sub(r"\s*\[C\d+(,\s*C\d+)*\]", "", text).strip(), "claims": sorted(set(ids))}
        if analysis:
            item["assumption"] = str(p.get("assumption", "")).strip() or "Not stated"
        out.append(item)
    return out


async def synthesize(llm: LLMProtocol, city: str, country: str, claims: list[dict[str, Any]],
                     gaps: dict[str, Any]) -> dict[str, Any]:
    if not claims:
        return {"executive_summary": [], "sections": {}, "opportunities": [], "risks": [],
                "meeting_questions": [], "dropped": ["No verified claims to synthesise."]}
    by_seq = {c["seq"]: c for c in claims}
    data = await llm.json(task="synthesize", system=SYSTEM, user=prompt(city, country, claims, gaps),
                          max_tokens=6000)
    dropped: list[str] = []
    sections = {}
    for d in DIMENSIONS:
        pts = _validate_points(((data.get("sections") or {}).get(d.key) or {}).get("points"), by_seq, dropped)
        if pts:
            sections[d.key] = {"points": pts}
    return {
        "executive_summary": _validate_points(data.get("executive_summary"), by_seq, dropped),
        "sections": sections,
        "opportunities": _validate_points(data.get("opportunities"), by_seq, dropped, analysis=True),
        "risks": _validate_points(data.get("risks"), by_seq, dropped, analysis=True),
        "meeting_questions": [str(q)[:300] for q in (data.get("meeting_questions") or [])][:10],
        "dropped": dropped,
    }

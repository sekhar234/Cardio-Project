"""Conflict detection between verified statistics (deterministic, no LLM).

Claims are grouped by (metric_key, geography level bucket). Within a group:
  * same period (or unknown period) and values differing by > 10% relative -> 'conflict':
    both values are kept and shown side by side with their sources; nothing is averaged or chosen.
  * different periods -> 'time_series': the most recent value is presented as current and the
    older values as history.
"""
from __future__ import annotations

from typing import Any

from ..taxonomy import CITY_LEVELS


def _bucket(c: dict[str, Any]) -> str:
    return "city" if not c.get("not_city_level") and c.get("geography_level") in CITY_LEVELS else \
        (c.get("geography_level") or "unknown")


def _year(c: dict[str, Any]) -> str:
    y = str(c.get("year") or "")
    return y[:4] if y[:4].isdigit() else ""


def find_conflicts(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for c in claims:
        if c.get("claim_type") != "statistic" or not c.get("metric_key") or c["metric_key"] == "other":
            continue
        if c.get("value") is None:
            continue
        groups.setdefault((c["metric_key"], _bucket(c), (c.get("unit") or "").lower().strip()), []).append(c)

    out = []
    for (metric, geo, unit), cs in groups.items():
        if len(cs) < 2:
            continue
        by_year: dict[str, list[dict[str, Any]]] = {}
        for c in cs:
            by_year.setdefault(_year(c), []).append(c)
        # conflicts: same year (or unknown year) with materially different values
        for year, same in by_year.items():
            vals = [float(c["value"]) for c in same]
            if len(same) >= 2 and max(vals) > 0 and (max(vals) - min(vals)) / max(vals) > 0.10:
                out.append({
                    "metric_key": metric, "geography_level": geo, "kind": "conflict",
                    "claim_ids": [c["id"] for c in same],
                    "description": (
                        f"Sources disagree on {metric.replace('_', ' ')} ({geo}, {year or 'period not stated'}): "
                        + "; ".join(f"{c['value']:g}{(' ' + c['unit']) if c.get('unit') else ''} [C{c['seq']}]"
                                    for c in same)
                        + ". Both are shown; neither is preferred without a methodological reason."
                    ),
                })
        years = sorted(y for y in by_year if y)
        if len(years) >= 2:
            ordered = [c for y in years for c in by_year[y]]
            out.append({
                "metric_key": metric, "geography_level": geo, "kind": "time_series",
                "claim_ids": [c["id"] for c in ordered],
                "description": (
                    f"{metric.replace('_', ' ').capitalize()} ({geo}) reported for several periods: "
                    + "; ".join(f"{_year(c)}: {c['value']:g}{(' ' + c['unit']) if c.get('unit') else ''} [C{c['seq']}]"
                                for c in ordered)
                    + f". Most recent ({years[-1]}) is treated as current."
                ),
            })
    return out

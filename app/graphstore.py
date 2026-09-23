"""Knowledge graph (Graphiti on Neo4j).

What lives here: the *relationships* between people, organisations, programmes, policies,
places and health conditions, as a temporal graph. Graphiti turns each episode (the verified
claims from one source) into entities and fact-edges, deduplicates entities across sources,
and time-stamps facts so that later runs can supersede earlier ones (institutional memory).

Only fact-checked claims are written, and every episode is registered in Postgres
(`graph_episodes`) so each graph fact can be traced back to its source document.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .config import settings

log = logging.getLogger(__name__)


# --- ontology ---------------------------------------------------------------------------------
class Person(BaseModel):
    """A named individual explicitly mentioned in a source (e.g. a mayor, health secretary, researcher)."""
    role_title: str | None = Field(None, description="Role or job title exactly as stated in the source")


class Organization(BaseModel):
    """A government body, hospital, university, NGO, company, network or professional society."""
    org_kind: str | None = Field(None, description="government | hospital | academic | ngo | private | multilateral | other")


class Programme(BaseModel):
    """A health programme, project, service or intervention (e.g. a hypertension screening programme)."""
    focus_area: str | None = Field(None, description="Condition or risk factor the programme targets")
    start_year: str | None = Field(None, description="Year the programme started, if stated")


class Policy(BaseModel):
    """A law, regulation, strategy, plan or official policy initiative."""
    jurisdiction_level: str | None = Field(None, description="city | state | national | international")
    adopted_year: str | None = Field(None, description="Year adopted, if stated")


class Place(BaseModel):
    """A city, district, state, country or other geographic area."""
    place_level: str | None = Field(None, description="district | city | metro | state | country")


class HealthCondition(BaseModel):
    """A disease or risk factor, e.g. hypertension, type 2 diabetes, stroke, dyslipidaemia, obesity."""


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Organization": Organization,
    "Programme": Programme,
    "Policy": Policy,
    "Place": Place,
    "HealthCondition": HealthCondition,
}

EXTRACTION_INSTRUCTIONS = (
    "The episode is a list of fact-checked statements about a city's cardiovascular health landscape. "
    "Extract only entities and relationships that are explicitly stated. Do not infer roles, "
    "affiliations, attitudes or relationships that are not written in the text. Keep people's names "
    "and titles exactly as written. If a statement is marked as national or regional data, attach it to "
    "the country/region, not to the city."
)


class GraphProtocol(Protocol):
    enabled: bool

    async def init(self) -> None: ...
    async def add_source_episode(self, *, city_slug: str, name: str, body: str, source_url: str,
                                 reference_time: dt.datetime) -> dict[str, Any]: ...
    async def search(self, query: str, city_slug: str, limit: int = 10) -> list[dict[str, Any]]: ...
    async def subgraph(self, city_slug: str, limit: int = 400) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class GraphStore:
    """Graphiti-backed implementation."""

    def __init__(self) -> None:
        self.enabled = bool(settings.neo4j_uri and settings.neo4j_password)
        self.g = None

    async def init(self) -> None:
        if not self.enabled or self.g is not None:
            return
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.driver.neo4j_driver import Neo4jDriver
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from openai import AsyncOpenAI

        # Graph extraction prompts are long; the SDK default timeout caused episode failures in production.
        oa = AsyncOpenAI(api_key=settings.openai_api_key, timeout=300, max_retries=3)

        llm_cfg = LLMConfig(api_key=settings.openai_api_key, model=settings.graph_model,
                            small_model=settings.graph_small_model)
        self.g = Graphiti(
            graph_driver=Neo4jDriver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password,
                                     database=settings.neo4j_database),
            llm_client=OpenAIClient(config=llm_cfg, client=oa),
            embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key=settings.openai_api_key,
                                                         embedding_model=settings.embedding_model), client=oa),
            cross_encoder=OpenAIRerankerClient(config=LLMConfig(api_key=settings.openai_api_key,
                                                                model=settings.graph_small_model)),
            max_coroutines=4,
        )
        await self.g.build_indices_and_constraints()

    async def add_source_episode(self, *, city_slug: str, name: str, body: str, source_url: str,
                                 reference_time: dt.datetime) -> dict[str, Any]:
        from graphiti_core.nodes import EpisodeType

        await self.init()
        res = await self.g.add_episode(
            name=name[:200],
            episode_body=body,
            source=EpisodeType.text,
            source_description=source_url,
            reference_time=reference_time,
            group_id=city_slug,
            entity_types=ENTITY_TYPES,
            custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
        )
        return {"episode_uuid": res.episode.uuid, "n_nodes": len(res.nodes), "n_edges": len(res.edges)}

    async def search(self, query: str, city_slug: str, limit: int = 10) -> list[dict[str, Any]]:
        await self.init()
        edges = await self.g.search(query, group_ids=[city_slug], num_results=limit)
        return [
            {
                "uuid": e.uuid,
                "fact": e.fact,
                "relation": e.name,
                "source_node_uuid": e.source_node_uuid,
                "target_node_uuid": e.target_node_uuid,
                "episodes": list(e.episodes or []),
                "valid_at": e.valid_at.isoformat() if e.valid_at else None,
                "invalid_at": e.invalid_at.isoformat() if e.invalid_at else None,
            }
            for e in edges
        ]

    async def subgraph(self, city_slug: str, limit: int = 400) -> dict[str, Any]:
        from graphiti_core.edges import EntityEdge
        from graphiti_core.nodes import EntityNode

        await self.init()
        nodes = await EntityNode.get_by_group_ids(self.g.driver, [city_slug], limit=limit)
        edges = await EntityEdge.get_by_group_ids(self.g.driver, [city_slug], limit=limit * 2)
        return {
            "nodes": [
                {
                    "id": n.uuid,
                    "name": n.name,
                    "type": next((lbl for lbl in n.labels if lbl != "Entity"), "Entity"),
                    "summary": n.summary,
                    "attributes": {k: v for k, v in (n.attributes or {}).items() if v},
                }
                for n in nodes
            ],
            "edges": [
                {
                    "id": e.uuid,
                    "source": e.source_node_uuid,
                    "target": e.target_node_uuid,
                    "relation": e.name,
                    "fact": e.fact,
                    "episodes": list(e.episodes or []),
                    "invalid_at": e.invalid_at.isoformat() if e.invalid_at else None,
                }
                for e in edges
            ],
        }

    async def close(self) -> None:
        if self.g is not None:
            await self.g.close()

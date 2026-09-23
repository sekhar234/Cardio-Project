"""Vector store (Qdrant).

Holds embeddings for semantic retrieval, and nothing that is the system of record:
  * kind="claim"   - each verified claim statement (points back to claims.id in Postgres)
  * kind="passage" - chunks of fetched source text (points back to sources.id)
Payloads carry only ids and short text; Postgres remains authoritative.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from .config import settings

log = logging.getLogger(__name__)
COLLECTION = "c4c_evidence"


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        # try to end on a sentence boundary
        cut = text.rfind(". ", start + size // 2, end)
        if cut != -1 and end < len(text):
            end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if len(c) > 40]


class VectorStore:
    def __init__(self, client: AsyncQdrantClient | None = None, dim: int | None = None):
        if client is not None:
            self.client = client
        elif settings.qdrant_url:
            self.client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None,
                                            timeout=60)
        else:
            self.client = AsyncQdrantClient(path=settings.qdrant_path)
        self.dim = dim or settings.embedding_dim
        self._ready = False

    async def ensure(self) -> None:
        if self._ready:
            return
        if not await self.client.collection_exists(COLLECTION):
            await self.client.create_collection(
                COLLECTION, vectors_config=qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE)
            )
        for field in ("city_slug", "run_id", "kind", "verdict"):
            try:
                await self.client.create_payload_index(COLLECTION, field, qm.PayloadSchemaType.KEYWORD)
            except Exception:  # noqa: BLE001 - index may already exist / local mode ignores
                pass
        self._ready = True

    async def upsert(self, items: list[dict[str, Any]], vectors: list[list[float]]) -> None:
        await self.ensure()
        points = [
            qm.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, it["key"])), vector=v, payload=it)
            for it, v in zip(items, vectors)
        ]
        for i in range(0, len(points), 128):
            await self.client.upsert(COLLECTION, points=points[i:i + 128])

    async def search(self, vector: list[float], city_slug: str, kind: str, limit: int = 8,
                     run_id: str | None = None) -> list[dict[str, Any]]:
        await self.ensure()
        must = [
            qm.FieldCondition(key="city_slug", match=qm.MatchValue(value=city_slug)),
            qm.FieldCondition(key="kind", match=qm.MatchValue(value=kind)),
        ]
        if run_id:
            must.append(qm.FieldCondition(key="run_id", match=qm.MatchValue(value=run_id)))
        if kind == "claim":
            must.append(qm.FieldCondition(key="verdict", match=qm.MatchAny(any=["supported", "partially_supported"])))
        res = await self.client.query_points(COLLECTION, query=vector, query_filter=qm.Filter(must=must),
                                             limit=limit, with_payload=True)
        return [{**(p.payload or {}), "score": p.score} for p in res.points]

    async def set_verdict(self, claim_keys_to_verdict: dict[str, str]) -> None:
        await self.ensure()
        for key, verdict in claim_keys_to_verdict.items():
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
            await self.client.set_payload(COLLECTION, payload={"verdict": verdict}, points=[pid])

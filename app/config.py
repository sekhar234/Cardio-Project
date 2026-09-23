"""Runtime configuration, read once from environment variables (or a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _normalise_db_url(url: str) -> str:
    # Render hands out postgres:// URLs; SQLAlchemy async needs an explicit driver.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@dataclass
class Settings:
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))

    # Research / extraction agents
    research_model: str = field(default_factory=lambda: os.getenv("RESEARCH_MODEL", "gpt-5-mini"))
    # The fact checker deliberately runs on a *different* model than the extractor, with its own
    # prompt and no access to the extractor's reasoning, so errors are less likely to be correlated.
    checker_model: str = field(default_factory=lambda: os.getenv("CHECKER_MODEL", "gpt-4.1"))
    # Model fallbacks, tried in order if a configured model is unavailable on the account.
    model_fallbacks: list[str] = field(
        default_factory=lambda: os.getenv("MODEL_FALLBACKS", "gpt-5-mini,gpt-4.1-mini,gpt-4o-mini").split(",")
    )
    graph_model: str = field(default_factory=lambda: os.getenv("GRAPH_MODEL", "gpt-5-mini"))
    graph_small_model: str = field(default_factory=lambda: os.getenv("GRAPH_SMALL_MODEL", "gpt-4.1-nano"))
    embedding_model: str = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"))
    embedding_dim: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_DIM", "1536")))

    # Datastores
    database_url: str = field(
        default_factory=lambda: _normalise_db_url(
            os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./local.db")
        )
    )
    qdrant_url: str = field(default_factory=lambda: os.getenv("QDRANT_URL", ""))
    qdrant_api_key: str = field(default_factory=lambda: os.getenv("QDRANT_API_KEY", ""))
    qdrant_path: str = field(default_factory=lambda: os.getenv("QDRANT_PATH", "./qdrant_local"))
    neo4j_uri: str = field(default_factory=lambda: os.getenv("NEO4J_URI", ""))
    neo4j_user: str = field(default_factory=lambda: os.getenv("NEO4J_USER", os.getenv("NEO4J_USERNAME", "neo4j")))
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", ""))

    # Research budget: the knobs that trade depth for time and cost.
    max_queries_per_round: int = field(default_factory=lambda: int(os.getenv("MAX_QUERIES_PER_ROUND", "16")))
    max_sources_per_round: int = field(default_factory=lambda: int(os.getenv("MAX_SOURCES_PER_ROUND", "18")))
    max_research_rounds: int = field(default_factory=lambda: int(os.getenv("MAX_RESEARCH_ROUNDS", "2")))
    max_source_chars: int = field(default_factory=lambda: int(os.getenv("MAX_SOURCE_CHARS", "14000")))
    llm_concurrency: int = field(default_factory=lambda: int(os.getenv("LLM_CONCURRENCY", "6")))
    fetch_timeout_s: float = field(default_factory=lambda: float(os.getenv("FETCH_TIMEOUT_S", "20")))

    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "CRAWLER_USER_AGENT",
            "Cardio4CitiesResearchBot/1.0 (+https://github.com/; research prototype; respects robots.txt)",
        )
    )
    robots_agent_token: str = "Cardio4CitiesResearchBot"


settings = Settings()

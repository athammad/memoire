"""SurrealDB connection and schema management."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from surrealdb.connections.async_ws import AsyncWsSurrealConnection

SURREAL_URL = "ws://localhost:8000"
SURREAL_USER = "root"
SURREAL_PASS = "root"
NAMESPACE = "memory"
DATABASE = "project_memory"

DAEMON_PORT = 7892


@asynccontextmanager
async def get_db():
    """Async context manager yielding an authenticated SurrealDB connection."""
    async with AsyncWsSurrealConnection(SURREAL_URL) as db:
        await db.connect()
        await db.signin({"username": SURREAL_USER, "password": SURREAL_PASS})
        await db.use(NAMESPACE, DATABASE)
        yield db


async def ensure_schema(db: AsyncWsSurrealConnection) -> None:
    """Create full-text search indexes and table definitions."""
    # v3: one FULLTEXT index per column, @@ operator, search::score(n) where n is @@ position
    statements = [
        "DEFINE ANALYZER IF NOT EXISTS memory_analyzer TOKENIZERS blank FILTERS lowercase, ascii",
        "DEFINE INDEX IF NOT EXISTS entity_name_idx ON TABLE entities COLUMNS name FULLTEXT ANALYZER memory_analyzer BM25",
        "DEFINE INDEX IF NOT EXISTS entity_summary_idx ON TABLE entities COLUMNS summary FULLTEXT ANALYZER memory_analyzer BM25",
        "DEFINE INDEX IF NOT EXISTS event_summary_idx ON TABLE events COLUMNS summary FULLTEXT ANALYZER memory_analyzer BM25",
        "DEFINE INDEX IF NOT EXISTS document_title_idx ON TABLE documents COLUMNS title FULLTEXT ANALYZER memory_analyzer BM25",
        "DEFINE INDEX IF NOT EXISTS document_summary_idx ON TABLE documents COLUMNS summary FULLTEXT ANALYZER memory_analyzer BM25",
        "DEFINE INDEX IF NOT EXISTS document_content_idx ON TABLE documents COLUMNS content FULLTEXT ANALYZER memory_analyzer BM25",
    ]
    for stmt in statements:
        await db.query(stmt)


def load_project_config(path: Path | None = None) -> dict:
    """Load .memory/config.json from the given path or cwd."""
    config_path = (path or Path.cwd()) / ".memory" / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"No .memory/config.json found at {config_path}. Run 'memory init' first."
        )
    return json.loads(config_path.read_text())

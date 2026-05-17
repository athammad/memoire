"""Core memory operations — store and retrieve structured knowledge."""

import datetime
from typing import Any

from surrealdb.connections.async_ws import AsyncWsSurrealConnection


async def store_entity(
    db: AsyncWsSurrealConnection,
    project_id: str,
    name: str,
    entity_type: str,
    summary: str,
) -> None:
    """Upsert an entity (file, module, concept) into memory."""
    await db.query(
        """
        UPSERT entities SET
            project_id = $project_id,
            name = $name,
            type = $type,
            summary = $summary,
            updated_at = time::now()
        WHERE project_id = $project_id AND name = $name
        """,
        {
            "project_id": project_id,
            "name": name,
            "type": entity_type,
            "summary": summary,
        },
    )


async def store_event(
    db: AsyncWsSurrealConnection,
    project_id: str,
    summary: str,
    importance: float,
    entities: list[str],
) -> None:
    """Store an episodic event (edit, discovery, decision)."""
    await db.create(
        "events",
        {
            "project_id": project_id,
            "summary": summary,
            "importance": importance,
            "entities": entities,
            "created_at": datetime.datetime.utcnow().isoformat(),
        },
    )


async def store_document(
    db: AsyncWsSurrealConnection,
    project_id: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
) -> None:
    """Upsert a document with full content (architecture note, spec, markdown file)."""
    summary = content[:500].strip()  # short digest for quick display
    await db.query(
        """
        UPSERT documents SET
            project_id = $project_id,
            title = $title,
            content = $content,
            summary = $summary,
            tags = $tags,
            updated_at = time::now()
        WHERE project_id = $project_id AND title = $title
        """,
        {
            "project_id": project_id,
            "title": title,
            "content": content,
            "summary": summary,
            "tags": tags or [],
        },
    )


async def store_relationship(
    db: AsyncWsSurrealConnection,
    project_id: str,
    source: str,
    relation: str,
    target: str,
) -> None:
    """Store a directional relationship between two named entities."""
    await db.query(
        """
        UPSERT relationships SET
            project_id = $project_id,
            source = $source,
            relation = $relation,
            target = $target
        WHERE project_id = $project_id AND source = $source
            AND relation = $relation AND target = $target
        """,
        {
            "project_id": project_id,
            "source": source,
            "relation": relation,
            "target": target,
        },
    )


async def search_memory(
    db: AsyncWsSurrealConnection,
    project_id: str,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Full-text search across entities, events, and documents."""
    results = []

    # search::score(n) — n is the 0-based position of the @@ expression in the WHERE clause
    entity_rows = await db.query(
        """
        SELECT name, type, summary, updated_at,
               search::score(0) + search::score(1) AS score
        FROM entities
        WHERE project_id = $project_id
            AND (name @@ $query OR summary @@ $query)
        ORDER BY score DESC
        LIMIT $limit
        """,
        {"project_id": project_id, "query": query, "limit": limit},
    )

    event_rows = await db.query(
        """
        SELECT summary, importance, entities, created_at,
               search::score(0) AS score
        FROM events
        WHERE project_id = $project_id
            AND summary @@ $query
        ORDER BY score DESC
        LIMIT $limit
        """,
        {"project_id": project_id, "query": query, "limit": limit},
    )

    document_rows = await db.query(
        """
        SELECT title, summary, tags, updated_at,
               search::score(0) + search::score(1) + search::score(2) AS score
        FROM documents
        WHERE project_id = $project_id
            AND (title @@ $query OR summary @@ $query OR content @@ $query)
        ORDER BY score DESC
        LIMIT $limit
        """,
        {"project_id": project_id, "query": query, "limit": limit},
    )

    for rows in (entity_rows, event_rows, document_rows):
        if isinstance(rows, list):
            results.extend(rows)

    return results


async def get_recent_events(
    db: AsyncWsSurrealConnection,
    project_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent episodic events for this project."""
    rows = await db.query(
        """
        SELECT summary, importance, entities, created_at
        FROM events
        WHERE project_id = $project_id
        ORDER BY created_at DESC
        LIMIT $limit
        """,
        {"project_id": project_id, "limit": limit},
    )
    return rows if isinstance(rows, list) else []


async def get_project_context(
    db: AsyncWsSurrealConnection,
    project_id: str,
) -> dict[str, Any]:
    """Assemble a compressed project overview for Claude."""
    entities = await db.query(
        """
        SELECT name, type, summary, updated_at
        FROM entities
        WHERE project_id = $project_id
        ORDER BY updated_at DESC
        LIMIT 30
        """,
        {"project_id": project_id},
    )

    events = await get_recent_events(db, project_id, limit=10)

    documents = await db.query(
        """
        SELECT title, summary, tags, updated_at
        FROM documents
        WHERE project_id = $project_id
        ORDER BY updated_at DESC
        LIMIT 10
        """,
        {"project_id": project_id},
    )

    relationships = await db.query(
        """
        SELECT source, relation, target
        FROM relationships
        WHERE project_id = $project_id
        LIMIT 50
        """,
        {"project_id": project_id},
    )

    return {
        "project_id": project_id,
        "entities": entities if isinstance(entities, list) else [],
        "recent_events": events,
        "documents": documents if isinstance(documents, list) else [],
        "relationships": relationships if isinstance(relationships, list) else [],
    }

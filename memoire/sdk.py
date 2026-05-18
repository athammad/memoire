"""Core memory operations — store and retrieve structured knowledge."""

import datetime
import math
from collections import defaultdict, deque
from typing import Any

from surrealdb.connections.async_ws import AsyncWsSurrealConnection


async def store_entity(
    db: AsyncWsSurrealConnection,
    project_id: str,
    name: str,
    entity_type: str,
    summary: str,
    side_effects: list[str] | None = None,
    writes_state: list[str] | None = None,
) -> None:
    """
    Upsert an entity (file, module, concept) into memory.

    side_effects: detected runtime side-effect categories (network, file_io,
                  subprocess, database, cache). Raises the cost-if-broken weight.
    writes_state: list of attribute names written via self.attr = ... Used to
                  infer mutation-driven DRIVES edges to importers.
    """
    await db.query(
        """
        UPSERT entities SET
            project_id = $project_id,
            name = $name,
            type = $type,
            summary = $summary,
            side_effects = $side_effects,
            writes_state = $writes_state,
            updated_at = time::now(),
            access_count = IF access_count THEN access_count ELSE 0 END
        WHERE project_id = $project_id AND name = $name
        """,
        {
            "project_id": project_id,
            "name": name,
            "type": entity_type,
            "summary": summary,
            "side_effects": side_effects or [],
            "writes_state": writes_state or [],
        },
    )


def _is_test_path(path: str) -> bool:
    """Return True if the path looks like a test file across all supported languages."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return (
        # Python
        stem.startswith("test_") or stem.endswith("_test")
        # TypeScript / JS
        or ".test." in name or ".spec." in name
        # Go
        or name.endswith("_test.go")
        # Rust
        or name.endswith("_test.rs")
        # Java
        or stem.endswith("Test") or stem.endswith("Spec") or stem.endswith("IT")
        # Ruby
        or name.endswith("_spec.rb") or name.endswith("_test.rb")
        # C / C++
        or stem.endswith("_test") or stem.startswith("test_")  # already covered above
        # Directory-based detection (all languages)
        or "tests" in parts or "test" in parts or "__tests__" in parts
        or "spec" in parts or "specs" in parts
        # Java Maven/Gradle layout
        or ("src" in parts and "test" in parts)
    )


async def promote_test_assertions(
    db: AsyncWsSurrealConnection,
    project_id: str,
) -> int:
    """
    Promote IMPORTS edges from test files into ASSERTS_ON causal edges.

    A test file that imports a module will fail when that module's behaviour
    changes — making it a high-cost causal dependent. This is re-run after
    every ingest so new tests are picked up automatically.

    Returns the number of ASSERTS_ON edges created or reinforced.
    """
    rows = await db.query(
        """
        SELECT source, target
        FROM relationships
        WHERE project_id = $project_id AND relation = 'IMPORTS'
        """,
        {"project_id": project_id},
    )
    if not isinstance(rows, list):
        return 0

    count = 0
    for row in rows:
        src, tgt = row.get("source", ""), row.get("target", "")
        if src and tgt and _is_test_path(src):
            await store_relationship(
                db, project_id,
                source=src,
                relation="ASSERTS_ON",
                target=tgt,
                rationale=f"Test file {src} imports {tgt} — changes to {tgt} will break this test",
                cost="high",
            )
            count += 1
    return count


async def promote_mutation_drives(
    db: AsyncWsSurrealConnection,
    project_id: str,
) -> int:
    """
    Create DRIVES edges for files that write mutable state read by their importers.

    Any file with detected self.attr = writes is a mutation source. Its importers
    depend on that state — a change to the write logic can silently corrupt
    downstream behaviour without a type error or import failure.

    Returns the number of DRIVES edges created or reinforced.
    """
    writers = await db.query(
        """
        SELECT name, writes_state
        FROM entities
        WHERE project_id = $project_id
            AND writes_state IS NOT NONE
        """,
        {"project_id": project_id},
    )
    if not isinstance(writers, list):
        return 0

    imports_rows = await db.query(
        """
        SELECT source, target
        FROM relationships
        WHERE project_id = $project_id AND relation = 'IMPORTS'
        """,
        {"project_id": project_id},
    )
    if not isinstance(imports_rows, list):
        return 0

    # Build importer map: module name / rel path → files that import it
    importer_map: dict[str, list[str]] = {}
    for row in imports_rows:
        tgt, src = row.get("target", ""), row.get("source", "")
        if src and tgt:
            importer_map.setdefault(tgt, []).append(src)

    count = 0
    for writer in writers:
        name = writer.get("name", "")
        attrs = writer.get("writes_state") or []
        if not attrs:
            continue

        # Match importers by rel path and by dotted module name
        module_key = name.replace("/", ".").removesuffix(".py")
        importers = list({
            *importer_map.get(name, []),
            *importer_map.get(module_key, []),
        })

        if not importers:
            continue

        attr_sample = ", ".join(f"self.{a}" for a in attrs[:3])
        rationale = (
            f"Mutates state ({attr_sample}) — "
            "importers reading these attributes break silently when write logic changes"
        )
        for importer in importers:
            await store_relationship(
                db, project_id,
                source=name,
                relation="DRIVES",
                target=importer,
                rationale=rationale,
            )
            count += 1
    return count


async def promote_high_fan_in_to_drives(
    db: AsyncWsSurrealConnection,
    project_id: str,
    threshold: int = 3,
) -> int:
    """
    Promote structurally central modules to causal DRIVES relationships.

    Any module imported by `threshold` or more files is considered a causal
    root — changing it forces changes in all its importers. Adds DRIVES edges
    from that module to each of its importers with an explanatory rationale.

    Returns the number of DRIVES edges created or updated.
    """
    rows = await db.query(
        """
        SELECT source, target
        FROM relationships
        WHERE project_id = $project_id AND relation = 'IMPORTS'
        """,
        {"project_id": project_id},
    )
    if not isinstance(rows, list):
        return 0

    # Count importers per target module
    importers: dict[str, list[str]] = {}
    for row in rows:
        src, tgt = row.get("source", ""), row.get("target", "")
        if src and tgt:
            importers.setdefault(tgt, []).append(src)

    count = 0
    for module, dependents in importers.items():
        if len(dependents) >= threshold:
            rationale = (
                f"Imported by {len(dependents)} files — "
                "high-fan-in module causally drives all its dependents"
            )
            for dependent in dependents:
                await store_relationship(
                    db, project_id,
                    source=module,
                    relation="DRIVES",
                    target=dependent,
                    rationale=rationale,
                )
                count += 1

    return count


async def touch_entity(
    db: AsyncWsSurrealConnection,
    project_id: str,
    name: str,
) -> None:
    """Increment access_count and refresh updated_at for a file entity."""
    await db.query(
        """
        UPDATE entities SET
            access_count = IF access_count THEN access_count + 1 ELSE 1 END,
            updated_at = time::now()
        WHERE project_id = $project_id AND name = $name
        """,
        {"project_id": project_id, "name": name},
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


_CAUSAL_RELATIONS = {"SPECIFIES", "IMPLEMENTS", "DRIVES", "DOCUMENTS", "ASSERTS_ON"}


async def store_relationship(
    db: AsyncWsSurrealConnection,
    project_id: str,
    source: str,
    relation: str,
    target: str,
    rationale: str = "",
    cost: str = "normal",
    extracted_from: str = "",
) -> None:
    """
    Store a directional relationship between two named entities.

    Each time the same (source, relation, target) triple is observed again —
    because the file was reprocessed — observations increments. High observations
    means high confidence: the graph learns which edges are stable vs transient.

    extracted_from: the file path that produced this edge via static analysis.
                    Empty for promotion-derived or temporally-inferred edges.
                    Used by prune_extracted_edges to remove stale edges after
                    a file is reprocessed and an import/class is removed.
    cost: "normal" | "high". High-cost edges surface first (test failures,
          side-effect chains) because their breakage has real-world consequences.
    """
    is_causal = relation in _CAUSAL_RELATIONS
    await db.query(
        """
        UPSERT relationships SET
            project_id = $project_id,
            source = $source,
            relation = $relation,
            target = $target,
            rationale = $rationale,
            is_causal = $is_causal,
            cost = $cost,
            extracted_from = $extracted_from,
            observations = IF observations THEN observations + 1 ELSE 1 END
        WHERE project_id = $project_id AND source = $source
            AND relation = $relation AND target = $target
        """,
        {
            "project_id": project_id,
            "source": source,
            "relation": relation,
            "target": target,
            "rationale": rationale,
            "is_causal": is_causal,
            "cost": cost,
            "extracted_from": extracted_from,
        },
    )


async def prune_extracted_edges(
    db: AsyncWsSurrealConnection,
    project_id: str,
    extracted_from: str,
    current_triples: list[tuple[str, str, str]],
) -> int:
    """
    Remove edges previously extracted from a file that no longer exist in it.

    Called after reprocessing a file. Any edge tagged with extracted_from that
    is not in current_triples was removed from the file (deleted import, removed
    class, etc.) and is now a ghost — delete it.

    Returns the number of edges pruned.
    """
    rows = await db.query(
        """
        SELECT source, relation, target
        FROM relationships
        WHERE project_id = $project_id AND extracted_from = $extracted_from
        """,
        {"project_id": project_id, "extracted_from": extracted_from},
    )
    if not isinstance(rows, list):
        return 0

    current_set = set(current_triples)
    pruned = 0
    for row in rows:
        triple = (row.get("source", ""), row.get("relation", ""), row.get("target", ""))
        if triple not in current_set:
            await db.query(
                """
                DELETE relationships
                WHERE project_id = $project_id
                    AND source = $source AND relation = $relation AND target = $target
                    AND extracted_from = $extracted_from
                """,
                {
                    "project_id": project_id,
                    "source": triple[0],
                    "relation": triple[1],
                    "target": triple[2],
                    "extracted_from": extracted_from,
                },
            )
            pruned += 1

    return pruned


async def delete_entity(
    db: AsyncWsSurrealConnection,
    project_id: str,
    name: str,
) -> None:
    """
    Remove a file entity and all relationships touching it from the graph.

    Called when a file is deleted from disk. Clears both the node and every
    edge where it appears as source or target so the graph stays consistent.
    """
    await db.query(
        "DELETE entities WHERE project_id = $project_id AND name = $name",
        {"project_id": project_id, "name": name},
    )
    await db.query(
        "DELETE documents WHERE project_id = $project_id AND title = $name",
        {"project_id": project_id, "name": name},
    )
    await db.query(
        """
        DELETE relationships
        WHERE project_id = $project_id AND (source = $name OR target = $name)
        """,
        {"project_id": project_id, "name": name},
    )


def _detect_causal_cycles(edges: list[tuple[str, str]]) -> list[str]:
    """
    DFS cycle detection on the causal edge set.

    Returns a list of human-readable cycle descriptions, one per cycle found.
    An empty list means the causal graph is a valid DAG.
    """
    adjacency: dict[str, list[str]] = defaultdict(list)
    all_nodes: set[str] = set()
    for src, tgt in edges:
        adjacency[src].append(tgt)
        all_nodes.update((src, tgt))

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in all_nodes}
    path: list[str] = []
    cycles: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbour in adjacency[node]:
            if color[neighbour] == GRAY:
                start = path.index(neighbour)
                cycles.append(" → ".join(path[start:]) + f" → {neighbour}")
            elif color[neighbour] == WHITE:
                dfs(neighbour)
        path.pop()
        color[node] = BLACK

    for node in all_nodes:
        if color[node] == WHITE:
            dfs(node)

    return cycles


async def detect_causal_cycles(
    db: AsyncWsSurrealConnection,
    project_id: str,
) -> list[str]:
    """
    Query all causal edges and return any cycles found in the graph.

    Causal edges should form a DAG (spec → code → docs). Cycles indicate a
    modelling error — e.g. a spec that DRIVES a module that SPECIFIES the spec.
    Returns cycle descriptions; empty list means the graph is a valid DAG.
    """
    rows = await db.query(
        """
        SELECT source, target
        FROM relationships
        WHERE project_id = $project_id AND is_causal = true
        """,
        {"project_id": project_id},
    )
    if not isinstance(rows, list):
        return []
    edges = [
        (r["source"], r["target"])
        for r in rows
        if r.get("source") and r.get("target")
    ]
    return _detect_causal_cycles(edges)


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


def _compute_causal_reachability(
    causal_edges: list[tuple[str, str]],
) -> dict[str, int]:
    """
    BFS from every node to count total nodes reachable via causal edges.

    A node with high reachability is a high-leverage root cause — changing it
    cascades through many downstream dependents. This is more accurate than
    degree count, which only measures direct neighbours.

    Returns {node_name: reachable_count}.
    """
    adjacency: dict[str, list[str]] = defaultdict(list)
    all_nodes: set[str] = set()

    for src, tgt in causal_edges:
        adjacency[src].append(tgt)
        all_nodes.update((src, tgt))

    reachability: dict[str, int] = {}
    for node in all_nodes:
        visited: set[str] = set()
        queue: deque[str] = deque([node])
        while queue:
            current = queue.popleft()
            for neighbour in adjacency[current]:
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append(neighbour)
        reachability[node] = len(visited)

    return reachability


def _causal_score(row: dict, causal_in: int, reachability: int) -> float:
    """
    Score an entity by causal reachability, side-effect cost, recency, and access frequency.

    reachability: total nodes reachable downstream via BFS on causal edges.
                  A spec that drives 10 files, each driving 2 more, has reachability=30.
                  Weighted 2× — root causes matter more than leaf effects.
    causal_in:    number of causal sources pointing at this entity. High in-degree
                  means heavily constrained — many things must change for this to change.
    side_effects: detected I/O categories. Each one adds weight — breakage in a file
                  that does network/database calls costs more than a pure function.
    """
    updated_at = row.get("updated_at")
    access_count = row.get("access_count") or 0
    side_effects = row.get("side_effects") or []

    age_days = 0.0
    if updated_at:
        try:
            ts = datetime.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            age_days = max((now - ts).total_seconds() / 86400, 0)
        except Exception:
            pass

    recency = math.exp(-age_days / 7)
    frequency = math.log1p(access_count)
    centrality = math.log1p(reachability * 2 + causal_in)
    side_effect_cost = math.log1p(len(side_effects)) * 0.5
    return recency + frequency + centrality + side_effect_cost


async def get_project_context(
    db: AsyncWsSurrealConnection,
    project_id: str,
) -> dict[str, Any]:
    """
    Return a compact, scored hierarchical overview of the project.

    The tree maps each directory to its immediate children (cheap — just paths).
    Semantic relationships are ranked by the activity score of their endpoints
    so the most relevant connections surface first. Call expand() for content.
    """
    contains_rows = await db.query(
        """
        SELECT source, target
        FROM relationships
        WHERE project_id = $project_id AND relation = 'CONTAINS'
        """,
        {"project_id": project_id},
    )

    tree: dict[str, list[str]] = {}
    if isinstance(contains_rows, list):
        for row in contains_rows:
            src, tgt = row.get("source"), row.get("target")
            if src and tgt:
                tree.setdefault(src, []).append(tgt)

    # Fetch all non-CONTAINS relationships — used for scoring and output
    all_rel_rows = await db.query(
        """
        SELECT source, relation, target, rationale, is_causal, cost, observations
        FROM relationships
        WHERE project_id = $project_id AND relation != 'CONTAINS'
        """,
        {"project_id": project_id},
    )
    all_rels = all_rel_rows if isinstance(all_rel_rows, list) else []

    # Compute true causal reachability (BFS) and in-degree from causal edges
    causal_edges = [
        (row.get("source", ""), row.get("target", ""))
        for row in all_rels
        if row.get("is_causal") and row.get("source") and row.get("target")
    ]
    reachability = _compute_causal_reachability(causal_edges)

    causal_in: dict[str, int] = {}
    for _, tgt in causal_edges:
        causal_in[tgt] = causal_in.get(tgt, 0) + 1

    # Fetch file entities with scoring fields (including side_effects)
    entity_rows = await db.query(
        """
        SELECT name, type, summary, updated_at, access_count, side_effects
        FROM entities
        WHERE project_id = $project_id AND type != 'directory'
        """,
        {"project_id": project_id},
    )

    scores: dict[str, float] = {}
    if isinstance(entity_rows, list):
        for row in entity_rows:
            name = row["name"]
            scores[name] = _causal_score(
                row,
                causal_in=causal_in.get(name, 0),
                reachability=reachability.get(name, 0),
            )

    # Rank relationships: endpoint score + causal bonus + cost bonus + confidence (observations)
    ranked_rels: list[dict] = []
    for row in all_rels:
        edge_score = scores.get(row.get("source", ""), 0) + scores.get(row.get("target", ""), 0)
        if row.get("is_causal"):
            edge_score += 1.0
        if row.get("cost") == "high":
            edge_score += 0.5
        # Confidence: edges seen many times are more reliable — log-scale boost
        edge_score += math.log1p(row.get("observations") or 1) * 0.3
        ranked_rels.append({**row, "_score": edge_score})

    ranked_rels.sort(key=lambda r: r["_score"], reverse=True)
    ranked_rels = [{k: v for k, v in r.items() if k != "_score"} for r in ranked_rels[:100]]

    events = await get_recent_events(db, project_id, limit=10)

    return {
        "project_id": project_id,
        "structure": tree,
        "relationships": ranked_rels,
        "recent_events": events,
    }


async def expand_node(
    db: AsyncWsSurrealConnection,
    project_id: str,
    path: str,
) -> dict[str, Any]:
    """
    Return full detail for a single directory or file node.

    For directories: lists children with their summaries.
    For files: returns the entity summary, document content (if any), and
    all semantic relationships (IMPORTS, INHERITS, etc.) touching this file.
    """
    entity_rows = await db.query(
        """
        SELECT name, type, summary, updated_at, side_effects, writes_state, access_count
        FROM entities
        WHERE project_id = $project_id AND name = $path
        LIMIT 1
        """,
        {"project_id": project_id, "path": path},
    )
    entity = entity_rows[0] if isinstance(entity_rows, list) and entity_rows else {}
    node_type = entity.get("type", "file")

    if node_type == "directory":
        child_rels = await db.query(
            """
            SELECT target
            FROM relationships
            WHERE project_id = $project_id AND relation = 'CONTAINS' AND source = $path
            """,
            {"project_id": project_id, "path": path},
        )
        children = [r["target"] for r in child_rels] if isinstance(child_rels, list) else []

        child_entities = await db.query(
            """
            SELECT name, type, summary
            FROM entities
            WHERE project_id = $project_id AND name INSIDE $names
            """,
            {"project_id": project_id, "names": children},
        )
        return {
            "path": path,
            "type": "directory",
            "children": child_entities if isinstance(child_entities, list) else [],
        }

    # File node — return summary, document content, and relationships
    doc_rows = await db.query(
        """
        SELECT title, content, summary, tags
        FROM documents
        WHERE project_id = $project_id AND title = $path
        LIMIT 1
        """,
        {"project_id": project_id, "path": path},
    )
    doc = doc_rows[0] if isinstance(doc_rows, list) and doc_rows else None

    rel_rows = await db.query(
        """
        SELECT source, relation, target, rationale, cost, observations
        FROM relationships
        WHERE project_id = $project_id
            AND relation != 'CONTAINS'
            AND (source = $path OR target = $path)
        ORDER BY observations DESC
        """,
        {"project_id": project_id, "path": path},
    )

    return {
        "path": path,
        "type": "file",
        "summary": entity.get("summary"),
        "side_effects": entity.get("side_effects") or [],
        "writes_state": entity.get("writes_state") or [],
        "document": doc,
        "relationships": rel_rows if isinstance(rel_rows, list) else [],
    }

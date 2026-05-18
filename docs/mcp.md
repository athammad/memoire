# MCP Tools Reference

memoire exposes four tools via the Model Context Protocol (MCP). Your AI assistant calls these automatically — you don't invoke them manually.

## `get_context`

Returns a compact, scored overview of the entire project. Call this at session start.

**Returns:**

```json
{
  "project_id": "myproject_a1b2c3d4",
  "structure": {
    ".": ["memoire/", "tests/", "README.md"],
    "memoire/": ["memoire/sdk.py", "memoire/processor.py", "..."]
  },
  "relationships": [
    {
      "source": "THEORY.md",
      "relation": "SPECIFIES",
      "target": "memoire/sdk.py",
      "rationale": "Theory defines the causal model the SDK must implement",
      "is_causal": true,
      "cost": "normal",
      "observations": 12
    }
  ],
  "recent_events": [
    {
      "summary": "Edited memoire/processor.py",
      "importance": 0.8,
      "created_at": "2026-05-18T10:23:00Z"
    }
  ]
}
```

**`structure`** — directory/file tree as a flat parent→children map. Full project shape in ~200 tokens regardless of size.

**`relationships`** — top 100 edges ranked by causal importance, recency, and confidence. Each edge is ~50 tokens. At 100 edges: ~5,000 tokens total.

**`recent_events`** — last 10 episodic events (file edits, bash commands). Orients the assistant to what changed recently.

---

## `expand`

Returns full detail for a single file or directory. Call this before opening a file to get its causal context and content without a raw file read.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `path` | string | Relative path from project root, e.g. `memoire/sdk.py` |

**Returns for a file:**

```json
{
  "path": "memoire/sdk.py",
  "type": "file",
  "summary": "Python file: memoire/sdk.py | side-effects: database",
  "side_effects": ["database"],
  "writes_state": ["token", "session"],
  "document": null,
  "relationships": [
    {
      "source": "memoire/sdk.py",
      "relation": "IMPORTS",
      "target": "surrealdb",
      "observations": 8
    }
  ]
}
```

**Returns for a directory:**

```json
{
  "path": "memoire/",
  "type": "directory",
  "children": [
    {"name": "memoire/sdk.py", "type": "file", "summary": "..."},
    {"name": "memoire/processor.py", "type": "file", "summary": "..."}
  ]
}
```

---

## `search`

Full-text search across all stored entities, documents, and events.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `query` | string | Search terms |

**Returns:** list of matching entities, documents, and events ranked by relevance score.

Use `search` before grepping or reading files — it covers all indexed content including markdown documents and file summaries.

---

## `recent_events`

Returns the most recent episodic events for the project.

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `limit` | integer | Maximum events to return (default: 20) |

**Returns:** list of events with `summary`, `importance`, `entities`, and `created_at`.

---

## Scoring

`get_context` returns relationships ranked by:

```
edge_score = score(source) + score(target) + causal_bonus + cost_bonus + confidence_boost
```

Where each node score is:

```
node_score = recency + frequency + centrality + side_effect_cost

recency        = exp(-age_days / 7)
frequency      = log1p(access_count)
centrality     = log1p(reachability × 2 + causal_in)
side_effect_cost = log1p(len(side_effects)) × 0.5
```

And the edge bonuses:

| Bonus | Value | Condition |
|---|---|---|
| `causal_bonus` | +1.0 | edge is causal (SPECIFIES, DRIVES, ASSERTS_ON, etc.) |
| `cost_bonus` | +0.5 | edge cost is `high` (ASSERTS_ON, side-effect chains) |
| `confidence_boost` | +log1p(observations) × 0.3 | scales with how many times edge was confirmed |

See [Theory & Design](theory.md) for the full rationale behind these choices.

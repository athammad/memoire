# Theory & Design

*This document explains the reasoning behind memoire's design — why a causal graph rather than a vector store, how scoring works, and what makes this different from existing tools.*

---

## 1. The starting-from-zero problem

Every session with an AI coding assistant begins with the same ritual: re-reading files, re-establishing context, re-discovering architecture. This isn't just wasteful — it's structurally limiting. The assistant can only reason about what it has read in the current context window. Anything outside that window doesn't exist for it.

The naive fix is persistent memory: store facts between sessions and load them at the start of the next one. Most memory systems for LLMs do exactly this — a flat list of facts, or a vector store of embeddings. They solve the re-reading problem but leave the deeper problem untouched.

The deeper problem is **reasoning about change**.

When a developer asks "what will break if I change this function?", the assistant needs to understand not just the project's structure but its *causal structure* — which changes propagate where, which dependencies are brittle, which failures have real-world consequences. Reading more files doesn't answer this. You need a model of causality.

---

## 2. Why structural graphs aren't enough

Existing code intelligence tools build structural graphs: call graphs, import graphs, inheritance hierarchies. These tell you *what is connected to what*. They cannot tell you *what will break*.

Consider a function `auth.create_token`. A structural graph tells you that `session.save`, `views.login_handler`, and `test_auth.test_login` are all reachable from it. But reachability is not risk:

- `session.save` writes `self.token` using the return value of `create_token`. If the token format changes, `session.save`'s write logic silently produces corrupt state. **This is high risk.**
- `views.login_handler` calls `session.save` which calls `create_token`. It's 2 hops away and makes a network call as a side effect. **Breakage here is high-cost.**
- `test_auth.test_login` asserts on the return value of `create_token`. **This test will fail deterministically.**
- `utils.format_date` is also reachable via 4 import hops, but asserts nothing, reads no state from auth, has no side effects. **Zero risk.**

A structural graph treats all four the same: reachable. A causal graph distinguishes them by *why* and *how badly* they break.

---

## 3. Two meanings of "causal"

memoire uses "causal" in two related but distinct senses.

### Intentional causality (design intent)

A project has a vertical causal hierarchy based on *design intent*:

```
Layer 0 — Ideas & specs
    ↓ SPECIFIES
Layer 1 — Core logic
    ↓ DRIVES
Layer 2 — Dependent logic
    ↓ DOCUMENTS
Layer 3 — Interface & docs
```

A design document *specifies* what a module must do. The module *implements* the design. Changes to the design *cause* changes in the module. This is intentional causality — it flows from human decisions.

This type of causality is extracted from natural language (design docs, markdown specs) using an LLM during ingestion.

### Consequential causality (runtime impact)

Within code, causality flows from the structure of execution:

- **Mutation causality**: A writes `self.token`, B reads it. Changing A's write logic causes B's assumptions to be violated.
- **Assertion causality**: A test asserts on the output of B. Changing B causes the test to fail.
- **Side-effect causality**: A calls B which triggers a network request. Breakage costs more than a pure function.
- **Structural causality**: A module imported by many files is a causal root.

This type of causality is inferred by static analysis — pattern matching on source code — not by running the code.

---

## 4. Graph structure

### Nodes

Every file, directory, and extracted concept is a node. Nodes carry:

- `type`: `file | directory | concept | decision`
- `side_effects`: detected runtime categories (`network`, `file_io`, `subprocess`, `database`, `cache`)
- `writes_state`: attribute names written via `self.attr = ...` (mutation sources)
- `access_count`: how many times the assistant has touched this file across sessions
- `updated_at`: last modification time

### Edges

Edges are directional and typed. Each edge carries:

- `relation`: semantic type
- `rationale`: one-sentence explanation of why this edge exists
- `is_causal`: whether this is a causal or structural edge
- `cost`: `normal | high`
- `observations`: how many times this edge has been re-confirmed by reprocessing
- `extracted_from`: which file produced this edge (enables pruning when the file changes)

### Edge types

| Relation | Causal? | Cost | Source |
|---|---|---|---|
| `SPECIFIES` | yes | normal | LLM extraction from markdown |
| `IMPLEMENTS` | yes | normal | LLM extraction from markdown |
| `DRIVES` | yes | normal | fan-in promotion, mutation detection, temporal sequences |
| `DOCUMENTS` | yes | normal | LLM extraction from markdown |
| `ASSERTS_ON` | yes | **high** | test file detection + import analysis |
| `IMPORTS` | no | normal | static analysis |
| `INHERITS` | no | normal | static analysis |
| `CONTAINS` | no | normal | file system traversal |

---

## 5. How the graph learns

### Observations as confidence

Every time memoire reprocesses a file, every edge extracted from that file is re-confirmed. The `observations` counter increments. An edge with `observations = 50` has been confirmed on 50 separate processings — it is structurally stable.

When ranking edges, `log1p(observations) × 0.3` is added to the score.

### Edge pruning

Every edge carries `extracted_from` — the file that produced it. When a file is reprocessed and an edge is no longer present (deleted import, removed class), that edge is deleted. The graph stays consistent as code evolves.

### Entity deletion

When a file is deleted from disk, the daemon removes its entity node and every relationship where it appears as source or target. No orphaned edges remain.

### Temporal causality

When the assistant edits file A and then file B within a 5-minute window, memoire infers that A *caused* B to need editing — a DRIVES edge. If the same pair appears across multiple sessions, their `observations` count grows and the edge becomes confident.

### Structural promotions

Three promotion rules run after every ingest and every 10 file changes:

1. **Fan-in promotion**: Any module imported by 3+ files gets DRIVES edges to all its importers.
2. **Test assertion promotion**: Any IMPORTS edge from a test file is promoted to ASSERTS_ON (cost: high).
3. **Mutation promotion**: Files with detected state writes get DRIVES edges to their importers.

### Cycle detection

The causal graph should be a DAG. After every ingest and promotion batch, a DFS traversal checks for cycles. Cycles (e.g. from contradictory LLM extractions or circular temporal inference) are logged as warnings.

---

## 6. Scoring

### Node score

```
score = recency + frequency + centrality + side_effect_cost

recency          = exp(-age_days / 7)
frequency        = log1p(access_count)
centrality       = log1p(reachability × 2 + causal_in)
side_effect_cost = log1p(len(side_effects)) × 0.5
```

`reachability` is computed by BFS from each node — the total number of nodes reachable downstream via causal edges. This is more accurate than out-degree: a core module with 3 direct importers each imported by 5 more files has out-degree 3 but reachability 18.

### Edge score

```
edge_score = score(source) + score(target) + causal_bonus + cost_bonus + confidence_boost

causal_bonus      = 1.0  if is_causal
cost_bonus        = 0.5  if cost == "high"
confidence_boost  = log1p(observations) × 0.3
```

---

## 7. Context compression

`get_context()` returns three things in ~5,500 tokens total:

1. **Structure** — directory/file tree (~200 tokens, full project shape)
2. **Relationships** — top 100 edges ranked by score (~5,000 tokens)
3. **Recent events** — last 10 episodic events (~200 tokens)

Compare to re-reading the project: a 20-file Python project typically runs 20,000–60,000 tokens.

When the assistant needs more detail, it calls `expand(path)` — full content and relationships for that specific node, only when actually needed.

---

## 8. Language coverage

Static analysis (consequential causality) is implemented for seven language families:

| Language | Side effects | State mutations | Test detection |
|---|---|---|---|
| Python | `requests`, `httpx`, `sqlite3`, `redis`, `subprocess`, `open()` | `self.attr = ...` | `test_*.py`, `*_test.py`, `tests/` |
| TypeScript / JS | `fetch`, `axios`, `fs.*`, `exec`, `spawn`, `prisma`, `mongoose` | `this.attr = ...` | `.test.ts`, `.spec.ts`, `__tests__/` |
| Go | `net/http`, `os.Create`, `exec.Command`, `database/sql` | — | `_test.go` |
| Rust | `reqwest`, `std::net`, `std::fs`, `Command::new`, `sqlx`, `diesel` | `self.field = ...` | `_test.rs`, `tests/` |
| Java | `java.net`, `HttpClient`, `java.io`, `ProcessBuilder`, `java.sql` | `this.field = ...` | `*Test.java`, `src/test/` |
| Ruby | `Net::HTTP`, `faraday`, `File.*`, `Open3`, `ActiveRecord`, `Redis` | `@attr = ...` | `_spec.rb`, `_test.rb`, `spec/` |
| C / C++ | `socket`, `fopen`, `system`, `popen`, `sqlite3_exec`, `curl_easy_*` | — | `test_*.c`, `*_test.cpp`, `tests/` |

Markdown and RST files feed the intentional causality layer via LLM extraction (provider-configurable).

---

## 9. Multi-provider architecture

The causal graph, scoring, and MCP server are provider-agnostic. Three integration layers vary by provider:

- **Instructions file**: tells the assistant to call `get_context` at session start
- **MCP config**: where the MCP server registration lives
- **LLM for markdown extraction**: which API produces intentional causal edges

See [Provider Setup](providers.md) for the full details.

---

## 10. What makes this different

Most knowledge graph tools for AI are either:

- **Generic fact stores** (Graphiti, Memento MCP): store any facts with temporal validity, not domain-specific to software
- **Statistical causal inference** (DoWhy): find cause-effect in datasets using statistical methods — unrelated to software structure
- **Structural code graphs** (code-graph-mcp, tree-sitter tools): map call and import edges — reachability without risk

memoire is specifically designed around one insight: **in a software project, the causal structure flows from design intent through implementation to documentation, and within code it flows through mutation, assertion, and side-effect dependencies.** Both layers must be captured to give an AI assistant the information it needs to make safe, targeted changes.

The graph is not a snapshot. It is a continuously learning model of the project's causal structure, where edge confidence grows with repeated observation and new causal patterns are discovered from how the project actually evolves.

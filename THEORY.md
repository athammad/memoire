# The Theory Behind memoire

## 1. The starting-from-zero problem

Every session with an AI coding assistant begins with the same ritual: re-reading files, re-establishing context, re-discovering architecture. This isn't just wasteful — it's structurally limiting. The assistant can only reason about what it has read in the current context window. Anything outside that window doesn't exist for it.

The naive fix is persistent memory: store facts between sessions and load them at the start of the next one. Most memory systems for LLMs do exactly this — a flat list of facts, or a vector store of embeddings. They solve the re-reading problem but leave the deeper problem untouched.

The deeper problem is **reasoning about change**.

When a developer asks "what will break if I change this function?", the assistant needs to understand not just the project's structure but its *causal structure* — which changes propagate where, which dependencies are brittle, which failures have real-world consequences. Reading more files doesn't answer this. You need a model of causality.

---

## 2. Why structural graphs aren't enough

Existing code intelligence tools build structural graphs: call graphs, import graphs, inheritance hierarchies. These tell you *what is connected to what*. They cannot tell you *what will break*.

Consider a function `auth.create_token`. A structural graph tells you that `session.save`, `views.login_handler`, and `test_auth.test_login` are all reachable from it via import and call edges. But reachability is not risk:

- `session.save` writes `self.token` using the return value of `create_token`. If the token format changes, `session.save`'s write logic silently produces corrupt state. **This is high risk.**
- `views.login_handler` calls `session.save` which calls `create_token`. It's 2 hops away and makes a network call as a side effect. **Breakage here is high-cost.**
- `test_auth.test_login` asserts on the return value of `create_token`. **This test will fail deterministically.**
- `utils.format_date` is also reachable via 4 import hops, but asserts nothing, reads no state from auth, has no side effects. **Zero risk.**

A structural graph treats all four the same: reachable. A causal graph distinguishes them by *why* and *how badly* they break.

---

## 3. Two meanings of "causal"

memoire uses "causal" in two related but distinct senses. It's worth being precise about both.

### 3.1 Intentional causality (design intent)

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

A design document *specifies* what a module must do. The module *implements* the design. Changes to the design *cause* changes in the module. Changes in the module *cause* changes in the documentation. This is intentional causality — it flows from human decisions and design choices, not from runtime execution.

This type of causality is extracted from natural language (design docs, markdown specs) using an LLM during ingestion. It answers: *why does this file exist? what concept is it an implementation of? what will need to change if the requirements change?*

### 3.2 Consequential causality (runtime impact)

Within code, causality flows from the structure of execution:

- **Mutation causality**: A writes `self.token`, B reads it. Changing A's write logic causes B's assumptions to be violated. The edge is A → B with relation DRIVES.
- **Assertion causality**: A test asserts on the output of B. Changing B causes the test to fail. The edge is test → B with relation ASSERTS_ON.
- **Side-effect causality**: A calls B which triggers a network request. If B changes, A's side effects may change. The cost of breakage is higher than a pure function.
- **Structural causality**: A module imported by many files is a causal root. Changing it forces re-evaluation of all its dependents. The edge is module → importers with relation DRIVES.

This type of causality is inferred by static analysis — pattern matching on source code — not by running the code. It answers: *what will break if I change this? how bad will the breakage be?*

---

## 4. The graph structure

### Nodes

Every file is a node. Every directory is a node. Concepts extracted from design documents are nodes. The project root is a node.

Nodes carry:
- `type`: `file | directory | concept | decision`
- `side_effects`: detected runtime categories (`network`, `file_io`, `subprocess`, `database`, `cache`)
- `writes_state`: list of attribute names written via `self.attr = ...` (mutation sources)
- `access_count`: how many times Claude has touched this file across sessions
- `updated_at`: last modification time

### Edges

Edges are directional and typed. Each edge carries:
- `relation`: the semantic type (see below)
- `rationale`: a one-sentence explanation of why this edge exists
- `is_causal`: whether this is a causal or structural edge
- `cost`: `normal | high` — high-cost edges involve side effects or test failures
- `observations`: how many times this edge has been re-confirmed by reprocessing

### Edge types

| Relation | Causal? | Cost | Source |
|---|---|---|---|
| `SPECIFIES` | yes | normal | LLM extraction from markdown |
| `IMPLEMENTS` | yes | normal | LLM extraction from markdown |
| `DRIVES` | yes | normal | fan-in promotion, mutation detection, temporal sequences, LLM |
| `DOCUMENTS` | yes | normal | LLM extraction from markdown |
| `ASSERTS_ON` | yes | **high** | test file detection + import analysis |
| `IMPORTS` | no | normal | static analysis |
| `INHERITS` | no | normal | static analysis |
| `CONTAINS` | no | normal | file system traversal |

---

## 5. How the graph learns

The graph is not static. It evolves as the codebase evolves, and it becomes more confident over time.

### Observations as confidence

Every time memoire reprocesses a file — because it was saved, or because Claude read or edited it — every edge extracted from that file is re-confirmed. The `observations` counter increments. After many sessions:

- An edge with `observations = 1` was seen once. It might be a transient pattern.
- An edge with `observations = 50` has been confirmed on 50 separate file processings. It is structurally stable.

When ranking edges, `log1p(observations) × 0.3` is added to the edge score. The graph learns to trust its own most stable patterns.

### Temporal causality

When Claude edits file A and then file B within a 5-minute window, memoire infers that A *caused* B to need editing — the same DRIVES relation. This is a rough heuristic, but it self-reinforces: if the same pair appears together across multiple sessions, their `observations` count grows and the inferred edge becomes confident.

If the pattern disappears (the files are no longer edited together), the edge stops being reinforced and ranks lower as newer, more confident edges appear.

### Edge pruning on reprocess

Every edge extracted by static analysis carries an `extracted_from` field — the file that produced it. When a file is reprocessed (on save or Claude edit), the new extraction produces a fresh set of edges. Any edge previously tagged with `extracted_from = file` that is not in the new set is deleted. This means:

- Remove an import statement → the IMPORTS edge is gone on next save
- Delete a class definition → its INHERITS edges disappear immediately
- Refactor away a mutation → the DRIVES edge it generated is pruned

Promotion-derived and temporally-inferred edges carry no `extracted_from` and are not pruned this way — they are governed by re-running the promotion rules.

### Entity deletion on file removal

When a file is deleted from disk, the daemon's filesystem watcher fires a deletion event. memoire immediately removes the entity node and every relationship where that file appears as source or target. No orphaned edges remain.

### Cycle detection

The causal graph is designed to be a DAG: ideas → code → docs, with no cycles. Cycles can appear from temporal inference (A edits B edits A in a loop) or from contradictory LLM extractions. After every ingest and every promotion batch, a DFS traversal checks all causal edges for cycles. Any cycle found is logged as a warning with its full path, so the user or a future maintenance pass can resolve the modelling contradiction.

### Structural promotions

Three promotion rules run after every ingest and every 10 file changes in the daemon:

1. **Fan-in promotion**: Any module imported by 3 or more files is promoted to a causal root. It gets DRIVES edges to all its importers with rationale explaining the fan-in count.

2. **Test assertion promotion**: Any IMPORTS edge whose source is a test file is promoted to an ASSERTS_ON edge with cost `high`. Tests are the most direct expression of consequential causality — a test that imports a module is explicitly asserting a contract on it.

3. **Mutation promotion**: Files with detected `self.attr = ...` writes are mutation sources. Any file that imports a mutation source gets a DRIVES edge from that source — because changing the write logic can silently corrupt state that the importer depends on.

---

## 6. Scoring

When Claude calls `get_context()`, the graph returns its most important nodes and edges first. Importance is scored by:

### Node score

```
score = recency + frequency + centrality + side_effect_cost
```

- `recency = exp(-age_days / 7)` — exponential decay with a 7-day half-life. A file last touched a week ago has half the recency score of one touched today.
- `frequency = log1p(access_count)` — logarithmic scaling of how often Claude has touched this file. Heavily used files are more important.
- `centrality = log1p(reachability × 2 + causal_in)` — BFS causal reachability (true downstream reach via graph traversal) weighted 2× over in-degree. A node that causes changes in 10 files through a dependency chain scores much higher than one directly imported by 3. This correctly ranks root-cause nodes (specs, core modules) above leaf effects, even when the causal path is indirect.
- `side_effect_cost = log1p(len(side_effects)) × 0.5` — files with network/database/file I/O side effects are harder to break safely.

**Why BFS reachability instead of out-degree?** Out-degree counts only direct dependencies. A core module with 3 direct importers, each imported by 5 more files, has out-degree 3 but BFS reachability 18. Using out-degree would underrank it. The graph traversal correctly identifies how much of the project a change could cascade into.

### Edge score

```
edge_score = score(source) + score(target) + causal_bonus + cost_bonus + confidence_boost
```

- `causal_bonus = 1.0` if the edge is causal — causal edges always surface above structural ones
- `cost_bonus = 0.5` if cost is `high` — test assertion and side-effect edges surface first
- `confidence_boost = log1p(observations) × 0.3` — stable edges rank above transient ones

The top 100 edges are returned, ordered by this score. Claude sees the most causally important, most confident, most recently active relationships first.

---

## 7. Context compression

The goal is to give Claude the maximum useful signal in the minimum tokens.

`get_context()` returns three things:

1. **Structure** — the directory/file tree as a flat map of parent → children. This gives the full project shape in ~200 tokens regardless of project size. No content, no summaries.

2. **Relationships** — the top 100 edges, ranked by the score above. Each edge is ~50 tokens (source, relation, target, rationale). At 100 edges: ~5,000 tokens. Claude gets the most important causal connections without reading any files.

3. **Recent events** — the last 10 episodic events (file edits, bash commands). ~200 tokens. Orients Claude to what changed recently.

Total: ~5,500 tokens for a complete, causally structured model of the project. Compare to re-reading the project: a 20-file Python project typically runs 20,000–60,000 tokens.

When Claude needs more detail, it calls `expand(path)` on a specific directory or file. This returns full content, all relationships, and side-effect/mutation metadata for that node — but only when actually needed, not by default.

---

## 8. Language coverage

The consequential causal layer (side effects, mutations, test assertions, import promotions) is implemented for three language families:

| Language | Side effects | State mutations | Test detection |
|---|---|---|---|
| Python | `requests`, `httpx`, `sqlite3`, `redis`, `subprocess`, `open()` | `self.attr = ...` | `test_*.py`, `*_test.py`, `tests/` |
| TypeScript / JS | `fetch`, `axios`, `fs.*`, `exec`, `spawn`, `prisma`, `mongoose` | `this.attr = ...` | `.test.ts`, `.spec.ts`, `__tests__/` |
| Go | `net/http`, `os.Create`, `exec.Command`, `database/sql` | — | `_test.go` |
| Rust | `reqwest`, `std::net`, `std::fs`, `Command::new`, `sqlx`, `diesel` | `self.field = ...` | `_test.rs`, `tests/` |
| Java | `java.net`, `HttpClient`, `java.io`, `ProcessBuilder`, `java.sql`, `JdbcTemplate` | `this.field = ...` | `*Test.java`, `src/test/` |
| Ruby | `Net::HTTP`, `faraday`, `File.*`, `Open3`, `ActiveRecord`, `Redis` | `@attr = ...` | `_spec.rb`, `_test.rb`, `spec/` |
| C / C++ | `socket`, `fopen`, `system`, `popen`, `sqlite3_exec`, `curl_easy_*` | — | `test_*.c`, `*_test.cpp`, `tests/` |

All three languages feed into the same causal graph. An edge extracted from a Go file and one from a TypeScript file can share a DRIVES relationship if they converge on the same node (e.g., a shared API contract or a config file).

Markdown and RST files feed the intentional causality layer via LLM extraction (the `claude` CLI). They are language-independent by nature — they describe intent, not implementation.

---

## 9. Multi-provider architecture

memoire is not tied to a single AI assistant. The causal graph, scoring, and MCP server are provider-agnostic. The three integration layers are:

### Integration layer 1 — Instructions file
Each provider reads project context from a different file. memoire writes the right one:
- Claude Code → `CLAUDE.md`
- Cursor → `.cursor/rules/memoire.mdc` (MDC format with frontmatter)
- Windsurf → `.windsurfrules`
- OpenAI Codex CLI → `AGENTS.md`
- Google Gemini CLI → `GEMINI.md`
- Ollama → no standard project instructions file (server only)

The instructions content is identical: call `get_context` at session start, use `expand()` before reading, use `search()` before grepping.

### Integration layer 2 — MCP server
The MCP server is the same across all providers. What differs is where the config lives:
- Claude Code → `.claude/settings.json`
- Cursor → `.cursor/mcp.json`
- Windsurf → `~/.codeium/windsurf/mcp_config.json` (global)
- Codex CLI → `.codex/config.toml`
- Gemini CLI → `.gemini/settings.json`
- Ollama → no standard MCP config (accessed via frontends like Continue.dev)

### Integration layer 3 — LLM for markdown extraction
Intentional causality (SPECIFIES, IMPLEMENTS, DRIVES, DOCUMENTS) is extracted from markdown by an LLM. The provider determines which API is called:

| Provider | API | Auth |
|---|---|---|
| claude | `claude --print` CLI | Claude Code OAuth |
| anthropic | Anthropic Messages API | `ANTHROPIC_API_KEY` |
| openai | OpenAI Chat Completions | `OPENAI_API_KEY` |
| gemini | Google Generative Language | `GEMINI_API_KEY` |
| ollama | Local Ollama at port 11434 | none |

The static analysis layer (imports, mutations, side effects, test assertions, BFS scoring) works identically regardless of provider — it has no LLM dependency and requires no API keys.

### Hook availability
Activity-based temporal causality (sequential edits → inferred DRIVES edges) relies on hooks: the IDE calling `memoire hook-event` after each tool use. This is currently only available in Claude Code. For other providers, the filesystem watcher tracks all file changes but cannot capture which assistant tool caused them.

---

## 10. What makes this different

Most knowledge graph tools for AI are either:

- **Generic fact stores** (Graphiti, Memento MCP): store any facts with temporal validity, not domain-specific to software
- **Statistical causal inference** (DoWhy, causal-inference MCPs): find cause-effect in datasets using statistical methods — unrelated to software structure
- **Structural code graphs** (code-graph-mcp, tree-sitter tools): map call and import edges — reachability without risk

memoire is specifically designed around one insight: **in a software project, the causal structure flows from design intent through implementation to documentation, and within code it flows through mutation, assertion, and side-effect dependencies.** Both layers must be captured to give an AI assistant the information it needs to make safe, targeted changes.

The graph is not a snapshot. It is a continuously learning model of the project's causal structure, where edge confidence grows with repeated observation and new causal patterns are discovered from how the project actually evolves.

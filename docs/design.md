# Design

*How memoire is implemented — scoring, context compression, ingestion, provider architecture, and language coverage.*

---

## Scoring

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

## Context compression

`get_context()` returns three things:

1. **Structure** — directory/file tree (~200 tokens, full project shape)
2. **Relationships** — top 100 edges ranked by score (~5,000–25,000 tokens depending on graph density)
3. **Recent events** — last 10 episodic events (~200 tokens)

Total context typically ranges from 6,000 to 25,000 tokens depending on project size and edge density. Compare to re-reading the project: a 20-file Python project typically runs 20,000–60,000 tokens; a project with PDFs and images can easily reach 300,000+ tokens.

When the assistant needs more detail, it calls `expand(path)` — full content and relationships for that specific node, only when actually needed.

The top-100 edge cap means the causal context stabilises well below 30,000 tokens regardless of project size. The structural-only view (Condition B in the benchmark) compresses to 2,000–7,000 tokens for most projects.

---

## Ingestion pipeline

### Code files

Static analysis extracts:
- Import dependencies → `IMPORTS` edges
- Inheritance → `INHERITS` edges
- Side-effect categories (network, file_io, subprocess, database, cache)
- State mutations (`self.attr = ...`, `this.attr = ...`) → later promoted to `DRIVES`
- Test-file detection → `IMPORTS` promoted to `ASSERTS_ON`

After each ingest run, three promotion rules fire:

1. **Fan-in promotion** — modules imported by 3+ files receive `DRIVES` edges to all importers
2. **Test assertion promotion** — test-file `IMPORTS` become `ASSERTS_ON` (high cost)
3. **Mutation promotion** — files with detected state writes receive `DRIVES` edges to importers

### Markdown and RST files

Full text is stored for full-text search. An LLM call extracts intentional causal edges: `SPECIFIES`, `IMPLEMENTS`, `DRIVES`, `DOCUMENTS`, `RELATES_TO` — each with a rationale sentence.

### PDF files

Extracted page-by-page using `pypdf`. Pages are concatenated with `--- Page N ---` separators and fed into the same LLM extraction pipeline as markdown. Requires `pip install "memoire-ai[pdf]"`.

### Image files (PNG, JPG, SVG, GIF, WebP)

Base64-encoded and sent to the provider's vision API (Claude by default) with a prompt requesting causal edges as JSON. Produces `SPECIFIES`, `DRIVES`, `DOCUMENTS`, and `RELATES_TO` edges describing what the diagram or screenshot depicts and how it relates to other project files.

Image extraction requires a provider with vision capability (`claude` or `openai`). Requires `pip install "memoire-ai[pdf]"` (includes Pillow).

---

## Language coverage

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

Markdown and RST files feed the intentional causality layer via LLM extraction. PDFs and images are also processed — see the Ingestion pipeline section above.

---

## Multi-provider architecture

The causal graph, scoring, and MCP server are provider-agnostic. Three integration layers vary by provider:

- **Instructions file**: tells the assistant to call `get_context` at session start
- **MCP config**: where the MCP server registration lives
- **LLM for markdown/PDF/image extraction**: which API produces intentional causal edges

| Provider | Instructions file | MCP config | Activity hooks | Extraction model |
|---|---|---|---|---|
| Claude Code | `CLAUDE.md` | `.claude/settings.json` | ✓ PostToolUse / PreToolUse | `claude --print` CLI |
| Cursor | `.cursor/rules/memoire.mdc` | `.cursor/mcp.json` | — | Anthropic API |
| Windsurf | `.windsurfrules` | `~/.codeium/windsurf/mcp_config.json` | — | Anthropic API |
| Codex CLI | `AGENTS.md` | `.codex/config.toml` | — | OpenAI API |
| Gemini CLI | `GEMINI.md` | `.gemini/settings.json` | — | Google API |
| Ollama | — | — | — | Local Ollama |

See [Provider Setup](providers.md) for the full configuration details.

---

## Daemon architecture

The memoire daemon is a long-running asyncio process with three subsystems running concurrently:

1. **Filesystem watcher** (`watchdog`) — monitors the project root for file changes. On change: re-processes the modified file, runs post-ingest promotions every 10 changes.
2. **Hook receiver** — an HTTP server (localhost) that receives `hook-event` and `pre-read` calls from the AI coding assistant's hooks. Writes episodic events to the graph.
3. **MCP server** — exposes `get_context`, `expand`, `search`, `recent_events` over the Model Context Protocol. Called by the assistant at session start and during operation.

All three share a single SurrealDB connection pool. The daemon is registered as a systemd user service (Linux) or LaunchAgent (macOS) via `memoire install-service` — it starts on login and restarts automatically if it crashes.

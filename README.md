# memoire

Persistent causal memory for AI coding assistants. Install it once — your assistant arrives at every session knowing not just what your project contains, but why things exist, what causes what, where changes will propagate, and what will break.

Works with Claude Code, Cursor, Windsurf, OpenAI, Gemini, and Ollama.

## The problem

Every Claude Code session starts from zero. Claude re-reads the same files, re-establishes the same context, re-discovers the same architecture. But the deeper problem is worse: even after re-reading everything, Claude still has to reason about impact from scratch — "if I change this function, what breaks?" — by reading code rather than understanding intent and consequence.

## The insight

A project has layers of causality. A design document specifies a module. That module drives its dependents. Changes cascade downward. And within code, a function that writes shared state causes silent failures in anything that reads it — failures that don't show up until runtime.

memoire builds a **causal knowledge graph** that captures this structure. Not just what imports what, but what causes what to change, what will fail if something breaks, and why files exist at all.

See [THEORY.md](THEORY.md) for the full design rationale.

## How it works

```
File changes + Claude activity
            ↓
    Background Daemon
    (watches files, captures hooks)
            ↓
        SurrealDB
    (local graph + full-text search)
            ↓
       MCP Server
            ↓
  Claude starts session with
  full causal project model — instantly
```

The graph evolves continuously. Every time a file is saved or Claude edits it, edges are re-observed and their confidence scores increase. Causal patterns that persist across many sessions become highly confident. Transient patterns fade.

## Relationship types

| Relation | Direction | Type | Meaning |
|---|---|---|---|
| `SPECIFIES` | idea → code | causal | this doc defines the intent this file implements |
| `IMPLEMENTS` | code → idea | causal | this file is the realization of that concept |
| `DRIVES` | core → dependent | causal | changing this will force changes in that |
| `DOCUMENTS` | doc → code | causal | this doc describes that file's behaviour |
| `ASSERTS_ON` | test → module | causal, high-cost | this test will fail if that module changes |
| `IMPORTS` | file → module | structural | static dependency, evidence for DRIVES |
| `INHERITS` | class → class | structural | inheritance hierarchy |
| `CONTAINS` | dir → child | structural | directory/file hierarchy |

Causal edges are ranked above structural ones. High-cost edges (`ASSERTS_ON`, side-effect chains) surface first — breakage there has real-world consequences.

## Does it actually save tokens?

Yes — in two distinct ways.

**No re-reading on session start.** Instead of Claude re-reading 20,000–50,000 tokens of files, it receives 2,000–5,000 tokens of structured context.

**Causal reasoning without file reads.** With a structural graph, Claude still has to open files to reason about impact. With a causal graph, "what do I need to change if I modify this spec?" is answered by traversing edges — no file reads. On a 20-file project, that saves 5,000–15,000 tokens per analysis.

Break-even: roughly 3 sessions on a project with 15+ files.

## Prerequisites

- Python 3.12+
- [SurrealDB](https://surrealdb.com/install) — started automatically if installed

```bash
curl -sSf https://install.surrealdb.com | sh
```

## Installation

```bash
pip install memoire-ai
```

## Quick start

```bash
memoire init --provider claude    # Claude Code (default)
memoire init --provider cursor    # Cursor
memoire init --provider windsurf  # Windsurf
memoire init --provider codex     # OpenAI Codex CLI
memoire init --provider gemini    # Google Gemini CLI
memoire init --provider ollama    # Ollama (LLM extraction only)

memoire ingest           # deep-read existing files and build the causal graph (skip if project is empty)
memoire install-service  # install as a system service — starts automatically on every login
```

Open a new session in your IDE. The assistant calls `get_context` automatically and arrives with the full causal model.

`memoire install-service` registers a systemd user service (Linux) or LaunchAgent (macOS). The daemon starts on login, restarts if it crashes, and works with any project — Ruby, Go, TypeScript, whatever. Run it once and never think about it again.

## Provider support

| Provider | Instructions file | MCP config | Activity hooks | Markdown LLM |
|---|---|---|---|---|
| Claude Code | `CLAUDE.md` | `.claude/settings.json` | ✓ PostToolUse / PreToolUse | `claude --print` CLI |
| Cursor | `.cursor/rules/memoire.mdc` | `.cursor/mcp.json` | — | Anthropic API (`ANTHROPIC_API_KEY`) |
| Windsurf | `.windsurfrules` | `~/.codeium/windsurf/mcp_config.json` | — | Anthropic API (`ANTHROPIC_API_KEY`) |
| Codex CLI | `AGENTS.md` | `.codex/config.toml` | — | OpenAI API (`OPENAI_API_KEY`) |
| Gemini CLI | `GEMINI.md` | `.gemini/settings.json` | — | Google API (`GEMINI_API_KEY`) |
| Ollama | — | — | — | Local Ollama at port 11434 |

For providers without hooks (Cursor, Windsurf, Codex, Gemini, Ollama), the filesystem watcher tracks all file changes — activity-based temporal causality inference is unavailable but the full static analysis graph and LLM markdown extraction work identically.

**API keys** — set the relevant environment variable before running `memoire ingest`:
```bash
export ANTHROPIC_API_KEY=...   # cursor, windsurf
export OPENAI_API_KEY=...      # codex
export GEMINI_API_KEY=...      # gemini
# ollama needs no key — runs at http://localhost:11434
```

## Commands

| Command | Description |
|---|---|
| `memoire init` | Initialise memoire in the current project |
| `memoire ingest` | Deep-read existing files — build full causal knowledge graph |
| `memoire start` | Start the daemon (daemonizes — survives terminal close) |
| `memoire stop` | Stop the running daemon |
| `memoire install-service` | Install as a system service — auto-starts on every login (recommended) |
| `memoire uninstall-service` | Remove the system service |
| `memoire check` | Diagnose the memoire setup — SurrealDB, config, provider files, API key, graph state |
| `memoire hook-event` | Called automatically by Claude Code hooks (internal) |
| `memoire pre-read` | Called automatically before Claude reads a file (internal) |
| `memoire mcp` | Start the MCP server (called automatically by Claude Code) |

## What Claude can query

**`get_context()`** — hierarchical project overview: directory/file tree, causal relationships ranked by centrality and confidence, recent events. Call at session start.

**`expand(path)`** — full detail for a directory or file. Includes side-effect categories, mutable state attributes, all causal and structural relationships with their confidence scores.

**`search(query)`** — full-text search across all stored knowledge.

**`recent_events(limit)`** — what changed recently.

## Causal scoring

Nodes are ranked by a composite score:

- **BFS causal reachability × 2** — true downstream reach via graph traversal (not degree count). A node that causes changes in 10 files through a chain scores higher than one directly imported by 3. Root-cause nodes (specs, core modules) score highest.
- **Causal in-degree** — how many causes point at this node.
- **Side-effect cost** — files that do network calls, database writes, or file I/O score higher because their breakage has real-world consequences.
- **Recency** — time-decay with a 7-day half-life.
- **Access frequency** — how often Claude has read or edited this file.

Edges are ranked by:
- **Observations** — how many times this edge has been re-confirmed by reprocessing. Stable edges (seen 20+ times) rank above transient ones. This is how the graph learns.
- **Causal bonus** — causal edges rank above structural ones.
- **Cost bonus** — high-cost edges (`ASSERTS_ON`, side-effect chains) rank first.

## Language support

| Language | Side effects | Mutations | Imports | Inheritance | Test assertions |
|---|---|---|---|---|---|
| Python | ✓ | ✓ `self.attr` | ✓ | ✓ | ✓ `test_*.py`, `*_test.py`, `tests/` |
| TypeScript / JS | ✓ | ✓ `this.attr` | ✓ | ✓ `extends` / `implements` | ✓ `.test.ts`, `.spec.ts`, `__tests__/` |
| Go | ✓ | — | ✓ | — | ✓ `_test.go` |
| Rust | ✓ | ✓ `self.field` | ✓ `use` | ✓ `impl Trait for` | ✓ `_test.rs`, `tests/` |
| Java | ✓ | ✓ `this.field` | ✓ `import` | ✓ `extends` / `implements` | ✓ `*Test.java`, `src/test/` |
| Ruby | ✓ | ✓ `@attr` | ✓ `require` | ✓ `class < Parent` | ✓ `_spec.rb`, `_test.rb`, `spec/` |
| C / C++ | ✓ | — | ✓ `#include` | ✓ `: public` | ✓ `test_*.c`, `*_test.cpp` |
| Markdown / RST | — | — | — | — | — |

All languages feed into the same causal graph with the same promotion rules: high-fan-in IMPORTS → DRIVES, test imports → ASSERTS_ON, mutation sources → DRIVES to importers.

Markdown files use LLM extraction (provider-configurable) to produce intentional causal edges: SPECIFIES, IMPLEMENTS, DRIVES, DOCUMENTS.

## What gets stored

**From Python files:**
- Import dependencies (IMPORTS) and class inheritance (INHERITS)
- Side-effect categories detected by pattern: `network`, `file_io`, `subprocess`, `database`, `cache`
- Mutable state attributes (`self.attr = ...`) — used to infer DRIVES edges to importers
- Test files (matching `test_*.py`, `*_test.py`, or in `tests/`) emit `ASSERTS_ON` edges for everything they import

**From TypeScript / JS files:**
- Import dependencies (IMPORTS), class inheritance (INHERITS), interface implementation (IMPLEMENTS)
- Same five side-effect categories detected by pattern
- Mutable state attributes (`this.attr = ...`) — same mutation-driven DRIVES inference
- Test files (`.test.ts`, `.spec.ts`, `__tests__/`) emit `ASSERTS_ON` edges for everything they import

**From Go files:**
- Import dependencies (IMPORTS) from single imports and import blocks
- Side-effect categories: `network`, `file_io`, `subprocess`, `database`
- Test files (`_test.go`) emit `ASSERTS_ON` edges for everything they import

**From markdown files:**
- Full content stored and indexed for search
- Claude extracts causal relationships: SPECIFIES, IMPLEMENTS, DRIVES, DOCUMENTS — with a rationale for each

**From Claude Code activity:**
- Sequential file edits within 5 minutes generate inferred DRIVES edges, reinforced on repetition
- Bash commands (git, pip, npm, pytest, etc.) stored as episodic events
- Every file read or edit bumps `access_count` and `observations` on related edges

**Structural promotions (run after every ingest and every 10 file changes):**
- High-fan-in IMPORTS → promoted to DRIVES (modules imported by 3+ files are causal roots)
- Test IMPORTS → promoted to ASSERTS_ON (high-cost)
- Mutation sources with importers → promoted to DRIVES (mutation-driven dependency)

**Graph integrity (Phase 3):**
- Every edge carries `extracted_from` — the file that produced it via static analysis
- When a file is reprocessed, edges no longer present in it are pruned (deleted import → edge removed)
- When a file is deleted from disk, its entity and all edges touching it are removed instantly
- Cycle detection runs after every ingest and promotion batch — causal edges must form a DAG; violations are logged as warnings

## Storage

All data is stored locally in SurrealDB — nothing leaves your machine. Each project has an isolated namespace.

## Project structure

```
.memory/
  config.json        # project namespace
.claude/
  settings.json      # hooks + MCP server (managed by memoire)
CLAUDE.md            # instructions for Claude (managed by memoire)
```

## Testing

The extraction and scoring logic is covered by a unit test suite:

```bash
pip install -e ".[dev]"
pytest tests/
```

135 tests covering: test-path detection across all 7 languages, side-effect detection, state mutation extraction, static extractors for Python/TypeScript/JS/Go/Rust/Java/Ruby/C/C++, BFS causal reachability, causal scoring, cycle detection, and LLM response parsing.

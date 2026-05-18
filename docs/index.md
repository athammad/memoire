# memoire

**Persistent causal project memory for AI coding assistants.**

Install it once — your assistant arrives at every session knowing not just what your project contains, but *why things exist*, *what causes what*, *where changes will propagate*, and *what will break*.

Works with **Claude Code**, **Cursor**, **Windsurf**, **OpenAI Codex CLI**, **Google Gemini CLI**, and **Ollama**.

---

## The problem

Every AI coding session starts from zero. The assistant re-reads the same files, re-establishes the same context, re-discovers the same architecture. But the deeper problem is worse: even after re-reading everything, it still has to reason about impact from scratch — *"if I change this function, what breaks?"* — by reading code rather than understanding intent and consequence.

## The insight

A project has layers of causality. A design document specifies a module. That module drives its dependents. Changes cascade downward. And within code, a function that writes shared state causes silent failures in anything that reads it.

memoire builds a **causal knowledge graph** that captures this structure. Not just what imports what, but what causes what to change, what will fail if something breaks, and why files exist at all.

## How it works

```
File changes + AI assistant activity
              ↓
      Background Daemon
   (watches files, captures hooks)
              ↓
          SurrealDB
    (local graph + full-text search)
              ↓
         MCP Server
              ↓
  Assistant starts session with
  full causal project model — instantly
```

The graph evolves continuously. Every time a file is saved or the assistant edits it, edges are re-observed and their confidence scores increase. Causal patterns that persist across many sessions become highly confident. Transient patterns fade.

## Does it save tokens?

Yes — in two distinct ways.

**No re-reading on session start.** Instead of re-reading 20,000–50,000 tokens of files, the assistant receives 2,000–5,000 tokens of structured causal context.

**Causal reasoning without file reads.** With a structural graph, the assistant still has to open files to reason about impact. With a causal graph, *"what do I need to change if I modify this spec?"* is answered by traversing edges — no file reads. On a 20-file project, that saves 5,000–15,000 tokens per analysis.

**Break-even:** roughly 3 sessions on a project with 15+ files.

---

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

Causal edges are ranked above structural ones. High-cost edges surface first — breakage there has real-world consequences.

---

## Storage

All data is stored **locally** in [SurrealDB](https://surrealdb.com). Nothing leaves your machine. Each project has an isolated namespace.

---

!!! tip "Ready to start?"
    Head to [Installation & Quick Start](quickstart.md) to get memoire running in under 5 minutes.

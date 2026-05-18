# Benchmark: memoire vs. baseline

This page documents the methodology and results of comparing a Claude Code session with memoire against a baseline session with no persistent memory.

---

## What we measure

Three questions are asked in both sessions:

1. **"What will break if I change the pandas version?"** — requires understanding dependency fan-in
2. **"What files do I need to touch to add a new forecast endpoint?"** — requires understanding causal chains
3. **"What are the riskiest files to modify in this project and why?"** — requires centrality reasoning

These questions were chosen because they require understanding *relationships* between files, not just reading individual files. They are exactly the cases where a causal graph wins.

---

## Sessions

### Baseline (no memoire)

Claude receives the raw content of every source file in the project concatenated into the prompt. This simulates what happens in a normal session where Claude re-reads files to establish context.

**Input:** all project files as raw text  
**File reads:** N (one per source file)  
**Context tokens:** proportional to total project size

### Memoire session

Claude receives the output of `get_context()` — the causal knowledge graph — instead of raw files.

**Input:** graph JSON (~2,000–5,000 tokens regardless of project size)  
**File reads:** 0  
**Context tokens:** fixed, independent of project size

---

## Model choices

memoire uses the **cheapest available model** for all its internal LLM tasks (markdown extraction during ingest):

| Provider | Extraction model | Input $/MTok | Output $/MTok |
|---|---|---|---|
| Claude / Anthropic | `claude-haiku-4-5` | $0.80 | $4.00 |
| OpenAI | `gpt-4o-mini` | $0.15 | $0.60 |
| Gemini | `gemini-2.0-flash` | $0.10 | $0.40 |
| Ollama | local model | $0.00 | $0.00 |

The benchmark measures the cost of the **session questions** (using whichever model you choose) plus the **amortised ingest cost** (Haiku by default, one-time per project spread across questions).

## How to run

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run against any memoire-initialised project (defaults to claude-haiku-4-5)
python scripts/benchmark.py --project-root /path/to/your/project

# Save full results to JSON
python scripts/benchmark.py --project-root /path/to/your/project --output results.json

# Run with a specific session model
python scripts/benchmark.py --project-root /path/to/your/project \
  --session-model claude-sonnet-4-6

# Use custom questions
python scripts/benchmark.py --project-root /path/to/your/project \
  --questions "What is the highest-risk file?" "What depends on the auth module?"
```

Requirements: `claude` CLI must be installed and authenticated.

---

## Example results

The following was run against the `testproject` — a realistic S&P 500 forecasting service with 28 source files (~19,000 tokens: FastAPI app, ARIMA pipeline, Redis cache, PostgreSQL ORM, Prometheus metrics, auth, scheduler, Streamlit dashboard, notifications, health checks, tests, docs).

| Question | Baseline tok | Memoire tok | Baseline $ | Memoire $ (w/ ingest) |
|---|---|---|---|---|
| What will break if I change the pandas version? | 19,267 | 7,565 | $0.01595 | $0.00938 |
| What files to touch for a new forecast endpoint? | 19,257 | 7,547 | $0.01590 | $0.00930 |
| What are the riskiest files to modify? | 19,409 | 7,800 | $0.01651 | $0.01031 |
| **Total** | **57,933** | **22,912** | **$0.04836** | **$0.02898** |

Session model: `claude-haiku-4-5` ($0.80/MTok in, $4.00/MTok out)  
Memoire extraction model: `claude-haiku-4-5` (one-time ingest cost: $0.00875, amortised above)

**Token reduction: 60.5%** | **Cost reduction: 40%** | **File reads: 28 → 0**

!!! note "Token count method"
    Token counts use a 1 token ≈ 4 characters approximation and costs are based on published pricing. Run the benchmark script on your own project for precise numbers.

---

## Why the savings grow with project size

The baseline cost scales linearly with project size — every file gets read. The memoire cost is roughly constant: `get_context()` returns a graph summary (~6,000–9,000 tokens) regardless of how large the project is.

| Project size | Avg file size | Baseline tokens | Memoire tokens | Reduction |
|---|---|---|---|---|
| 15 files (tiny) | ~350 tok/file | ~5,000–7,000 | ~7,000–9,000 | ~0% or negative |
| 30 files | ~500 tok/file | ~15,000 | ~8,000 | ~47% |
| 50 files | ~600 tok/file | ~30,000 | ~8,000 | ~73% |
| 100 files | ~800 tok/file | ~80,000 | ~8,000 | ~90% |
| 200 files | ~1,000 tok/file | ~200,000+ | ~8,000 | ~96% |

Break-even is roughly a project whose source files total more than ~10,000 tokens (~25+ average-sized files). For very small demo projects memoire still eliminates file reads and speeds up responses — it just does not save input tokens until the project grows.

---

## Answer quality

Token savings are only meaningful if the answers are as good or better. The causal graph answers the dependency questions *more precisely* than file reading because:

- **Pandas fan-in is explicit** — the graph already computed that `pandas` drives 3 source files. Claude doesn't have to grep for imports.
- **Risk is pre-scored** — nodes are ranked by causal reachability × side-effect cost. The riskiest files surface immediately.
- **Causal chains are pre-traced** — "what do I need to touch to add an endpoint?" is answered by traversing DRIVES edges, not reading every file looking for patterns.

The baseline session typically produces a correct but unranked answer ("you'd need to change these files"). The memoire session produces a ranked, rationale-bearing answer ("change `pipeline.py` first because it has the highest causal reachability and two network side effects").

---

## Running your own benchmark

```bash
memoire init --provider claude
memoire ingest
python scripts/benchmark.py --project-root . --output my_results.json
```

Share results at [github.com/athammad/memoire/discussions](https://github.com/athammad/memoire/discussions).

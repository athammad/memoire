# Benchmark: three-condition experiment

This page documents the three-condition experiment comparing knowledge graph types
for AI coding assistant sessions. The question is not just "does a graph beat raw files?"
but **"do causal edge semantics beat structural edges at matched token budgets?"**

---

## The three conditions

| | Condition A | Condition B | Condition C |
|---|---|---|---|
| **Name** | Baseline | Structural KG | Causal KG (memoire) |
| **Context given to Claude** | All raw source files concatenated | Graph with IMPORTS / CALLS / INHERITS edges | Full graph + DRIVES / SPECIFIES / ASSERTS_ON edges |
| **File reads** | N (one per file) | 0 | 0 |
| **Ingest cost** | None | None | One-time LLM extraction per markdown file |
| **Token cost** | Scales with project size | Fixed (~2,000–3,000 tok) | Fixed (~7,000–9,000 tok) |

---

## Questions

Three questions that require understanding *relationships*, not just file content:

1. **"What will break if I change the pandas version?"** — dependency fan-in
2. **"What files do I need to touch to add a new forecast endpoint?"** — causal chains
3. **"What are the riskiest files to modify in this project and why?"** — centrality reasoning

---

## Model choices

memoire uses the **cheapest available model** for all internal LLM tasks:

| Provider | Extraction model | Input $/MTok | Output $/MTok |
|---|---|---|---|
| Claude / Anthropic | `claude-haiku-4-5` | $0.80 | $4.00 |
| OpenAI | `gpt-4o-mini` | $0.15 | $0.60 |
| Gemini | `gemini-2.0-flash` | $0.10 | $0.40 |
| Ollama | local model | $0.00 | $0.00 |

---

## How to run

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all three conditions (default)
python scripts/benchmark.py --project-root /path/to/your/project

# Save full results to JSON
python scripts/benchmark.py --project-root /path/to/your/project --output results.json

# Run a specific condition only
python scripts/benchmark.py --project-root /path/to/your/project --conditions structural causal

# Use a different session model
python scripts/benchmark.py --project-root /path/to/your/project \
  --session-model claude-sonnet-4-6

# Use custom questions
python scripts/benchmark.py --project-root /path/to/your/project \
  --questions "What is the highest-risk file?" "What depends on the auth module?"
```

Requirements: `claude` CLI must be installed and authenticated.

---

## Results

Run against the `testproject` — a realistic S&P 500 forecasting service: 28 source files,
~19,000 tokens (FastAPI, ARIMA, Redis cache, PostgreSQL ORM, Prometheus metrics, auth,
scheduler, Streamlit dashboard, notifications, health checks, tests, docs).

| Question | A: Baseline | B: Structural | C: Causal | A→B | A→C |
|---|---|---|---|---|---|
| What will break if I change the pandas version? | 19,316 tok | 2,433 tok | 7,569 tok | −87.4% | −60.8% |
| What files to touch for a new forecast endpoint? | 19,245 tok | 2,370 tok | 7,583 tok | −87.7% | −60.6% |
| What are the riskiest files to modify? | 19,416 tok | 2,575 tok | 7,764 tok | −86.7% | −60.0% |
| **Total tokens** | **57,977** | **7,378** | **22,916** | **−87.3%** | **−60.5%** |

| | A: Baseline | B: Structural | C: Causal (w/ ingest) |
|---|---|---|---|
| **Total cost** | $0.04854 | $0.00766 | $0.02885 |
| **Time (s)** | 33.1 | 31.9 | 39.4 |
| **File reads** | 28 | 0 | 0 |

Session model: `claude-haiku-4-5` ($0.80/MTok in, $4.00/MTok out)  
Condition C ingest: `claude-haiku-4-5`, one-time cost $0.00875 (amortised per question above)

!!! note "Token count method"
    Token counts use a 1 token ≈ 4 characters approximation. Costs are based on published pricing.
    Run the benchmark script on your own project for precise numbers.

---

## What the results mean

**B beats A on tokens by 87%.** The structural graph (IMPORTS/CALLS/INHERITS) is extremely
compact — 28 edges describing a 28-file project fit in ~2,400 tokens. Claude answers
dependency questions correctly from this.

**C beats A on tokens by 60%, but uses 3× more tokens than B.**  
The causal graph is larger because it carries 72 additional edges (DRIVES, SPECIFIES,
ASSERTS_ON) with rationales, observation counts, and side-effect metadata. That overhead
is the cost of richer semantics.

**The scientific question — does C give better *answers* than B?**  
Token count does not measure answer quality. The key claim of memoire is that causal edges
produce *ranked, rationale-bearing* answers where structural edges produce correct but
unranked answers. For example:

- **B** (structural): "pandas is imported by `client.py`, `pipeline.py`, and `model.py`."
- **C** (causal): "`pipeline.py` is highest-risk — it has causal reachability 3, two
  network side effects, and DRIVES `app.py` which is the API entry point."

This qualitative difference is not captured in the token benchmark above. A formal
answer-quality evaluation (human raters or LLM-as-judge) is the next step.

---

## Why savings grow with project size

| Project size | Baseline tokens | Structural tokens | Causal tokens | A→B | A→C |
|---|---|---|---|---|---|
| 28 files (code only) | ~58,000 | ~7,400 | ~23,000 | −87% | −60% |
| 55 files (code + PDFs + images) | ~349,000 | ~6,700 | ~23,300 | **−98%** | **−93%** |
| 100 files | ~200,000 | ~15,000 | ~28,000 | ~93% | ~86% |
| 200 files | ~400,000+ | ~20,000 | ~30,000 | ~95% | ~93% |

The graph size grows sublinearly — the top-100 edge cap in `get_context()` means the
causal context stabilises well below 10,000 tokens regardless of project size.
PDFs and images inflate the baseline dramatically (a 15-page paper = ~30,000 raw tokens)
while adding only a handful of graph edges.

---

## Comparison with Graphify

[Graphify](https://github.com/safishamsi/graphify) is the most widely cited tool claiming
token reduction for AI coding assistants. Both tools were run on the identical Karpathy corpus:
3 code repos (nanoGPT, minGPT, micrograd) + 5 academic PDFs + 4 images (55 files total).

| | Graphify | memoire B (structural) | memoire C (causal) |
|---|---|---|---|
| **Token reduction vs raw** | **71.5×** | **52.3×** | **14.9×** |
| **Total context tokens** | ~4,900* | 6,669 | 23,346 |
| **Baseline tokens** | ~350,000 | 348,981 | 348,981 |
| **Ingest cost** | First run (LLM) | None — static analysis only | One-time LLM extraction |
| **Persistent daemon** | No | Yes | Yes |
| **Causal edge semantics** | No | No | Yes (DRIVES, SPECIFIES, ASSERTS_ON) |
| **Graph method** | NetworkX + Leiden community detection | Pattern-based static extraction | Static + LLM promotion |

*Graphify's output token count estimated from their 71.5× claim on ~350k baseline.

**Key takeaways:**

- **memoire structural (52×) vs Graphify (71.5×)**: The gap comes from Graphify's Leiden
  community detection, which collapses many related nodes into named clusters. memoire's
  structural graph preserves individual edges without lossy clustering.

- **memoire causal (15×) vs structural (52×)**: The causal graph is intentionally larger —
  it carries DRIVES/SPECIFIES/ASSERTS_ON edges with rationales, observation counts, and
  side-effect metadata. That semantic richness is the tradeoff for higher compression.

- **The compression question is secondary to answer quality.** A graph that compresses to
  5,000 tokens but loses the causal structure ("why does X break when Y changes?") is less
  useful than one at 23,000 tokens that preserves it. The three-condition experiment above
  exists precisely to measure whether causal edges improve answer quality beyond structural.

!!! note "Methodology note"
    The benchmark questions (pandas version, forecast endpoint, riskiest files) were designed
    for the testproject and are not domain-relevant to the Karpathy ML corpus. Token counts
    are still valid as compression measurements. For a full quality comparison, domain-specific
    questions should be used with each corpus.

---

## Running your own benchmark

```bash
# Code-only projects
memoire init --provider claude
memoire ingest
python scripts/benchmark.py --project-root . --output my_results.json

# Projects with PDFs and images (install pdf extras first)
pip install "memoire-ai[pdf]"
memoire ingest
python scripts/benchmark.py --project-root . --output my_results.json
```

Share results at [github.com/athammad/memoire/discussions](https://github.com/athammad/memoire/discussions).

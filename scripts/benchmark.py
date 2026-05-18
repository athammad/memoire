"""
Benchmark: memoire vs. baseline Claude session.

Measures token usage, file reads, and estimated cost for a set of standard
questions asked:
  - WITHOUT memoire (Claude must discover context by reading files)
  - WITH memoire    (Claude receives get_context() output upfront)

Usage:
    python scripts/benchmark.py --project-root /path/to/project
    python scripts/benchmark.py --project-root /path/to/project --output results.json
    python scripts/benchmark.py --project-root /path/to/project --session-model claude-haiku-4-5
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Standard questions — chosen to require dependency and impact reasoning
# ---------------------------------------------------------------------------

QUESTIONS = [
    "What will break if I change the pandas version?",
    "What files do I need to touch to add a new forecast endpoint?",
    "What are the riskiest files to modify in this project and why?",
]

# ---------------------------------------------------------------------------
# Model pricing — $ per million tokens (input, output)
# Keep in sync with memoire's _LLM_DEFAULTS
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Claude (Anthropic)
    "claude-haiku-4-5":           (0.80,   4.00),
    "claude-haiku-4-5-20251001":  (0.80,   4.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-7":            (15.00, 75.00),
    # OpenAI
    "gpt-4o-mini":                (0.15,   0.60),
    "gpt-4o":                     (2.50,  10.00),
    # Google
    "gemini-2.0-flash":           (0.10,   0.40),
    "gemini-1.5-flash":           (0.075,  0.30),
    "gemini-1.5-pro":             (1.25,   5.00),
}

MEMOIRE_EXTRACTION_MODEL = "claude-haiku-4-5"   # matches _LLM_DEFAULTS


def _cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Return estimated cost in USD for the given token counts and model."""
    price_in, price_out = MODEL_PRICING.get(model, (3.00, 15.00))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 characters."""
    return len(text) // 4


def _read_project_files(root: Path) -> dict[str, str]:
    """Read all source files a baseline Claude would have to read."""
    extensions = {".py", ".ts", ".js", ".go", ".rs", ".java", ".rb", ".md", ".toml", ".txt", ".json"}
    skip_dirs = {".git", ".memory", "__pycache__", "node_modules", ".venv", "venv"}
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in extensions:
            if not any(part in skip_dirs for part in path.parts):
                try:
                    files[str(path.relative_to(root))] = path.read_text(errors="ignore")
                except Exception:
                    pass
    return files


def _ask_claude(prompt: str) -> tuple[str, float]:
    """Call `claude --print` with the prompt. Returns (response_text, elapsed_seconds)."""
    t0 = time.monotonic()
    result = subprocess.run(
        ["claude", "--print"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.monotonic() - t0
    return result.stdout.strip(), elapsed


def _get_memoire_context(root: Path) -> str:
    """Call the memoire SDK get_project_context and return the JSON string."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from memoire.db import get_db
    from memoire.sdk import get_project_context
    import json as _json

    config_path = root / ".memory" / "config.json"
    if not config_path.exists():
        raise RuntimeError(f"No memoire config at {config_path}. Run 'memoire init' first.")
    config = _json.loads(config_path.read_text())
    project_id = config["project_id"]

    async def _fetch():
        async with get_db() as db:
            return await get_project_context(db, project_id)

    result = asyncio.run(_fetch())
    return _json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Session runners
# ---------------------------------------------------------------------------

def run_baseline(root: Path, questions: list[str], model: str) -> list[dict]:
    """Baseline: give Claude the raw file contents, then ask each question."""
    print("\n[benchmark] Running BASELINE session (no memoire)...")
    files = _read_project_files(root)

    file_context = "\n\n".join(
        f"=== {name} ===\n{content}" for name, content in files.items()
    )
    total_file_tokens = _count_tokens(file_context)
    print(f"  Files read: {len(files)}  (~{total_file_tokens:,} tokens of context)")

    results = []
    for i, question in enumerate(questions, 1):
        print(f"  Q{i}: {question}")
        prompt = (
            "You are a software engineer. Here are all the project files:\n\n"
            f"{file_context}\n\n"
            f"Question: {question}\n"
            "Answer concisely."
        )
        response, elapsed = _ask_claude(prompt)
        input_tokens = _count_tokens(prompt)
        output_tokens = _count_tokens(response)
        estimated_cost = _cost(input_tokens, output_tokens, model)
        results.append({
            "question": question,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "elapsed_seconds": round(elapsed, 2),
            "file_reads": len(files),
            "model": model,
            "response_preview": response[:300],
        })
        print(f"    → {input_tokens:,} in + {output_tokens:,} out tokens, {elapsed:.1f}s, ${estimated_cost:.5f}")

    return results


def run_memoire(root: Path, questions: list[str], model: str) -> list[dict]:
    """Memoire: give Claude the get_context() output, then ask each question."""
    print("\n[benchmark] Running MEMOIRE session...")
    try:
        context = _get_memoire_context(root)
    except Exception as e:
        print(f"  ERROR fetching memoire context: {e}")
        return []

    context_tokens = _count_tokens(context)

    # Estimate the one-time ingest cost: memoire calls the LLM once per markdown file
    md_files = [f for f in _read_project_files(root) if f.endswith(".md")]
    ingest_input_tokens = sum(
        _count_tokens(content) + 200  # 200 tokens for the extraction prompt overhead
        for content in [_read_project_files(root).get(f, "") for f in md_files]
    )
    ingest_output_tokens = len(md_files) * 300  # ~300 tokens of JSON edges per file
    ingest_cost = _cost(ingest_input_tokens, ingest_output_tokens, MEMOIRE_EXTRACTION_MODEL)

    print(f"  Graph context: ~{context_tokens:,} tokens  (no file reads)")
    print(f"  One-time ingest cost: ${ingest_cost:.5f} ({len(md_files)} markdown files × {MEMOIRE_EXTRACTION_MODEL})")

    results = []
    for i, question in enumerate(questions, 1):
        print(f"  Q{i}: {question}")
        prompt = (
            "You are a software engineer. Here is the causal knowledge graph for this project "
            "(produced by memoire — a persistent causal memory tool):\n\n"
            f"{context}\n\n"
            f"Question: {question}\n"
            "Answer concisely using the graph. Do not ask to read files."
        )
        response, elapsed = _ask_claude(prompt)
        input_tokens = _count_tokens(prompt)
        output_tokens = _count_tokens(response)
        session_cost = _cost(input_tokens, output_tokens, model)
        results.append({
            "question": question,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": round(session_cost, 6),
            "elapsed_seconds": round(elapsed, 2),
            "file_reads": 0,
            "model": model,
            "ingest_cost_usd": round(ingest_cost / len(questions), 6),  # amortised per question
            "ingest_model": MEMOIRE_EXTRACTION_MODEL,
            "response_preview": response[:300],
        })
        print(f"    → {input_tokens:,} in + {output_tokens:,} out tokens, {elapsed:.1f}s, ${session_cost:.5f}")

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(baseline: list[dict], memoire: list[dict]) -> None:
    """Print a comparison table to stdout."""
    print("\n" + "=" * 80)
    print("  BENCHMARK RESULTS")
    print("=" * 80)

    if not baseline or not memoire:
        print("  Incomplete results — one or both sessions failed.")
        return

    b_total_tok  = sum(r["total_tokens"] for r in baseline)
    m_total_tok  = sum(r["total_tokens"] for r in memoire)
    b_total_cost = sum(r["estimated_cost_usd"] for r in baseline)
    m_total_cost = sum(r["estimated_cost_usd"] for r in memoire)
    m_ingest     = sum(r.get("ingest_cost_usd", 0) for r in memoire)
    b_time       = sum(r["elapsed_seconds"] for r in baseline)
    m_time       = sum(r["elapsed_seconds"] for r in memoire)
    tok_savings  = b_total_tok - m_total_tok
    cost_savings = b_total_cost - (m_total_cost + m_ingest)
    tok_pct      = tok_savings / b_total_tok * 100 if b_total_tok else 0
    model        = baseline[0]["model"]

    print(f"\n  Model: {model}")
    print(f"  Memoire extraction model: {memoire[0].get('ingest_model', MEMOIRE_EXTRACTION_MODEL)}\n")

    hdr = f"  {'Question':<46} {'B.Tok':>7} {'M.Tok':>7} {'B.Cost':>8} {'M.Cost':>8}"
    print(hdr)
    print(f"  {'-'*46} {'-'*7} {'-'*7} {'-'*8} {'-'*8}")
    for b, m in zip(baseline, memoire):
        q = b["question"][:44]
        m_total_q = m["estimated_cost_usd"] + m.get("ingest_cost_usd", 0)
        print(f"  {q:<46} {b['total_tokens']:>7,} {m['total_tokens']:>7,} "
              f"  ${b['estimated_cost_usd']:>6.5f}   ${m_total_q:>6.5f}")

    print(f"\n  {'TOTAL':<46} {b_total_tok:>7,} {m_total_tok:>7,} "
          f"  ${b_total_cost:>6.5f}   ${m_total_cost + m_ingest:>6.5f}")
    print(f"  {'TIME (s)':<46} {b_time:>7.1f} {m_time:>7.1f}")
    print(f"\n  Token reduction:  {tok_pct:.1f}%  ({tok_savings:,} tokens saved)")
    print(f"  Cost reduction:   ${cost_savings:.5f} saved per {len(baseline)}-question session")
    print(f"  File reads:       baseline={baseline[0]['file_reads']} files → memoire=0 files")
    print(f"\n  Note: memoire cost includes amortised ingest (${m_ingest:.5f} spread across questions).")
    print(f"        Ingest is one-time per project — cost per session drops with each reuse.")
    print("=" * 80 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the memoire benchmark."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-root", required=True, help="Path to the project to benchmark")
    parser.add_argument("--output", default=None, help="Write full results to this JSON file")
    parser.add_argument("--questions", nargs="+", default=None, help="Override the default questions")
    parser.add_argument(
        "--session-model",
        default="claude-haiku-4-5",
        choices=list(MODEL_PRICING.keys()),
        help="Model used for the benchmark session questions (default: claude-haiku-4-5)",
    )
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    questions = args.questions or QUESTIONS
    model = args.session_model

    if not shutil.which("claude"):
        print("ERROR: 'claude' CLI not found. Install Claude Code to run this benchmark.")
        sys.exit(1)

    baseline = run_baseline(root, questions, model)
    memoire_results = run_memoire(root, questions, model)
    print_report(baseline, memoire_results)

    if args.output:
        b_tok  = sum(r["total_tokens"] for r in baseline)
        m_tok  = sum(r["total_tokens"] for r in memoire_results)
        b_cost = sum(r["estimated_cost_usd"] for r in baseline)
        m_cost = sum(r["estimated_cost_usd"] for r in memoire_results)
        m_ing  = sum(r.get("ingest_cost_usd", 0) for r in memoire_results)
        out = {
            "project": str(root),
            "session_model": model,
            "extraction_model": MEMOIRE_EXTRACTION_MODEL,
            "questions": questions,
            "baseline": baseline,
            "memoire": memoire_results,
            "summary": {
                "baseline_total_tokens": b_tok,
                "memoire_total_tokens": m_tok,
                "token_savings": b_tok - m_tok,
                "token_reduction_pct": round((b_tok - m_tok) / b_tok * 100, 1) if b_tok else 0,
                "baseline_cost_usd": round(b_cost, 6),
                "memoire_cost_usd": round(m_cost + m_ing, 6),
                "cost_savings_usd": round(b_cost - (m_cost + m_ing), 6),
                "baseline_file_reads": baseline[0]["file_reads"] if baseline else 0,
                "memoire_file_reads": 0,
            },
        }
        Path(args.output).write_text(json.dumps(out, indent=2))
        print(f"Full results written to {args.output}")


if __name__ == "__main__":
    import shutil
    main()

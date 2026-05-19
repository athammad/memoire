"""
Benchmark: three-condition experiment for memoire.

Condition A — BASELINE   : Claude receives raw concatenated file contents.
Condition B — STRUCTURAL : Claude receives a knowledge graph with structural
                           edges only (IMPORTS, CALLS, INHERITS — no causal).
Condition C — CAUSAL     : Claude receives the full memoire causal graph
                           (adds DRIVES, SPECIFIES, ASSERTS_ON, DOCUMENTS).

This mirrors the three-condition experiment described in the memoire paper draft.
The key scientific question: do causal edge semantics improve answer quality
and/or token efficiency beyond a structural-only graph at matched token budgets?

Usage:
    python scripts/benchmark.py --project-root /path/to/project
    python scripts/benchmark.py --project-root /path/to/project --output results.json
    python scripts/benchmark.py --project-root /path/to/project --session-model claude-sonnet-4-6
"""

import argparse
import asyncio
import json
import shutil
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

# Edge types considered structural vs causal (mirrors sdk._CAUSAL_RELATIONS)
_STRUCTURAL_RELATIONS = {"IMPORTS", "CALLS", "INHERITS", "REFERENCES", "CONTAINS"}
_CAUSAL_RELATIONS     = {"DRIVES", "SPECIFIES", "IMPLEMENTS", "DOCUMENTS", "ASSERTS_ON"}

# ---------------------------------------------------------------------------
# Model pricing — $ per million tokens (input, output)
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5":           (0.80,   4.00),
    "claude-haiku-4-5-20251001":  (0.80,   4.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-7":            (15.00, 75.00),
    "gpt-4o-mini":                (0.15,   0.60),
    "gpt-4o":                     (2.50,  10.00),
    "gemini-2.0-flash":           (0.10,   0.40),
    "gemini-1.5-flash":           (0.075,  0.30),
    "gemini-1.5-pro":             (1.25,   5.00),
}

MEMOIRE_EXTRACTION_MODEL = "claude-haiku-4-5"


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
    """Read all source files a baseline Claude would have to read.

    Handles text files, PDFs (via pypdf), and images (via claude --print vision).
    This mirrors what Graphify's baseline does — the full raw content of every file.
    """
    text_extensions = {
        ".py", ".ts", ".js", ".go", ".rs", ".java", ".rb",
        ".md", ".toml", ".txt", ".json", ".svg",
    }
    pdf_extensions  = {".pdf"}
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    all_extensions = text_extensions | pdf_extensions | image_extensions

    skip_dirs = {".git", ".memory", "__pycache__", "node_modules", ".venv", "venv"}
    files = {}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in all_extensions:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue

        key = str(path.relative_to(root))

        if path.suffix in text_extensions:
            try:
                files[key] = path.read_text(errors="ignore")
            except Exception:
                pass

        elif path.suffix in pdf_extensions:
            try:
                import pypdf  # type: ignore
                reader = pypdf.PdfReader(str(path))
                pages = []
                for i, page in enumerate(reader.pages, 1):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append(f"--- Page {i} ---\n{text}")
                if pages:
                    files[key] = "\n\n".join(pages)
            except ImportError:
                print("  WARNING: pypdf not installed — PDFs excluded from baseline. Run: pip install pypdf")
            except Exception as exc:
                print(f"  WARNING: could not extract {key}: {exc}")

        elif path.suffix in image_extensions:
            try:
                import base64
                raw = path.read_bytes()
                b64 = base64.b64encode(raw).decode()
                mime = "image/png" if path.suffix == ".png" else "image/jpeg"
                prompt = (
                    "Describe every concept, entity, diagram element, label, and relationship "
                    f"visible in this image concisely.\n\n[image:{mime};base64,{b64}]"
                )
                description, _ = _ask_claude(prompt)
                if description:
                    files[key] = description
            except Exception as exc:
                print(f"  WARNING: could not describe image {key}: {exc}")

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


def _get_full_context(root: Path) -> dict:
    """Fetch the full memoire project context (structural + causal edges) as a dict."""
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

    return asyncio.run(_fetch())


def _structural_context(full: dict) -> dict:
    """Filter a full context dict down to structural edges only (no causal edges).

    Returns a new dict with the same shape as get_project_context() but with
    DRIVES / SPECIFIES / ASSERTS_ON / DOCUMENTS / IMPLEMENTS edges removed.
    This simulates what tools like CodexGraph or Aider RepoMap would provide.
    """
    structural_rels = [
        r for r in full.get("relationships", [])
        if r.get("relation") not in _CAUSAL_RELATIONS
    ]
    return {
        **full,
        "relationships": structural_rels,
    }


def _ingest_cost(root: Path) -> float:
    """Estimate the one-time ingest cost for markdown files in the project."""
    all_files = _read_project_files(root)
    md_files = {k: v for k, v in all_files.items() if k.endswith(".md")}
    ingest_input_tokens = sum(_count_tokens(c) + 200 for c in md_files.values())
    ingest_output_tokens = len(md_files) * 300
    return _cost(ingest_input_tokens, ingest_output_tokens, MEMOIRE_EXTRACTION_MODEL)


# ---------------------------------------------------------------------------
# Session runners
# ---------------------------------------------------------------------------

def run_baseline(root: Path, questions: list[str], model: str) -> list[dict]:
    """Condition A: give Claude the raw file contents, then ask each question."""
    print("\n[benchmark] Condition A — BASELINE (raw files)...")
    files = _read_project_files(root)
    file_context = "\n\n".join(f"=== {name} ===\n{content}" for name, content in files.items())
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
        input_tokens  = _count_tokens(prompt)
        output_tokens = _count_tokens(response)
        estimated_cost = _cost(input_tokens, output_tokens, model)
        results.append({
            "condition": "baseline",
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


def run_structural(root: Path, questions: list[str], model: str) -> list[dict]:
    """Condition B: structural knowledge graph only (IMPORTS/CALLS/INHERITS, no causal edges)."""
    print("\n[benchmark] Condition B — STRUCTURAL graph (no causal edges)...")
    try:
        full = _get_full_context(root)
    except Exception as e:
        print(f"  ERROR fetching context: {e}")
        return []

    structural = _structural_context(full)
    context_str = json.dumps(structural, indent=2, default=str)
    context_tokens = _count_tokens(context_str)
    n_total = len(full.get("relationships", []))
    n_structural = len(structural.get("relationships", []))
    n_causal_dropped = n_total - n_structural
    print(f"  Graph context: ~{context_tokens:,} tokens  ({n_structural} structural edges, {n_causal_dropped} causal edges removed)")

    results = []
    for i, question in enumerate(questions, 1):
        print(f"  Q{i}: {question}")
        prompt = (
            "You are a software engineer. Here is a structural knowledge graph for this project "
            "(file imports, calls, inheritance — no causal relationships):\n\n"
            f"{context_str}\n\n"
            f"Question: {question}\n"
            "Answer concisely using the graph. Do not ask to read files."
        )
        response, elapsed = _ask_claude(prompt)
        input_tokens  = _count_tokens(prompt)
        output_tokens = _count_tokens(response)
        session_cost  = _cost(input_tokens, output_tokens, model)
        results.append({
            "condition": "structural",
            "question": question,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": round(session_cost, 6),
            "elapsed_seconds": round(elapsed, 2),
            "file_reads": 0,
            "model": model,
            "edges_structural": n_structural,
            "edges_causal_dropped": n_causal_dropped,
            "response_preview": response[:300],
        })
        print(f"    → {input_tokens:,} in + {output_tokens:,} out tokens, {elapsed:.1f}s, ${session_cost:.5f}")
    return results


def run_causal(root: Path, questions: list[str], model: str) -> list[dict]:
    """Condition C: full memoire causal graph (structural + DRIVES/SPECIFIES/ASSERTS_ON)."""
    print("\n[benchmark] Condition C — CAUSAL graph (full memoire)...")
    try:
        full = _get_full_context(root)
    except Exception as e:
        print(f"  ERROR fetching memoire context: {e}")
        return []

    context_str    = json.dumps(full, indent=2, default=str)
    context_tokens = _count_tokens(context_str)
    ingest_cost    = _ingest_cost(root)
    n_rels = len(full.get("relationships", []))
    n_causal = sum(1 for r in full.get("relationships", []) if r.get("relation") in _CAUSAL_RELATIONS)

    print(f"  Graph context: ~{context_tokens:,} tokens  ({n_rels} edges, {n_causal} causal)")
    print(f"  One-time ingest cost: ${ingest_cost:.5f} ({MEMOIRE_EXTRACTION_MODEL})")

    results = []
    for i, question in enumerate(questions, 1):
        print(f"  Q{i}: {question}")
        prompt = (
            "You are a software engineer. Here is the causal knowledge graph for this project "
            "(produced by memoire — includes DRIVES, SPECIFIES, ASSERTS_ON causal edges):\n\n"
            f"{context_str}\n\n"
            f"Question: {question}\n"
            "Answer concisely using the graph. Do not ask to read files."
        )
        response, elapsed = _ask_claude(prompt)
        input_tokens  = _count_tokens(prompt)
        output_tokens = _count_tokens(response)
        session_cost  = _cost(input_tokens, output_tokens, model)
        results.append({
            "condition": "causal",
            "question": question,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": round(session_cost, 6),
            "elapsed_seconds": round(elapsed, 2),
            "file_reads": 0,
            "model": model,
            "ingest_cost_usd": round(ingest_cost / len(questions), 6),
            "ingest_model": MEMOIRE_EXTRACTION_MODEL,
            "edges_total": n_rels,
            "edges_causal": n_causal,
            "response_preview": response[:300],
        })
        print(f"    → {input_tokens:,} in + {output_tokens:,} out tokens, {elapsed:.1f}s, ${session_cost:.5f}")
    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(
    baseline: list[dict],
    structural: list[dict],
    causal: list[dict],
) -> None:
    """Print a three-condition comparison table to stdout."""
    print("\n" + "=" * 100)
    print("  BENCHMARK RESULTS  (A: Baseline | B: Structural KG | C: Causal KG)")
    print("=" * 100)

    if not baseline or not structural or not causal:
        print("  Incomplete results — one or more conditions failed.")
        return

    def _totals(results: list[dict]) -> tuple[int, float, float, float]:
        tok  = sum(r["total_tokens"] for r in results)
        cost = sum(r["estimated_cost_usd"] for r in results)
        ing  = sum(r.get("ingest_cost_usd", 0) for r in results)
        t    = sum(r["elapsed_seconds"] for r in results)
        return tok, cost, ing, t

    b_tok, b_cost, _,    b_time = _totals(baseline)
    s_tok, s_cost, _,    s_time = _totals(structural)
    c_tok, c_cost, c_ing, c_time = _totals(causal)
    model = baseline[0]["model"]

    print(f"\n  Session model: {model}")
    print(f"  Extraction model: {MEMOIRE_EXTRACTION_MODEL}  (causal ingest only)\n")

    hdr = f"  {'Question':<44} {'A.Tok':>7} {'B.Tok':>7} {'C.Tok':>7}  {'A.$':>8} {'B.$':>8} {'C.$':>8}"
    print(hdr)
    print(f"  {'-'*44} {'-'*7} {'-'*7} {'-'*7}  {'-'*8} {'-'*8} {'-'*8}")

    for a, b, c in zip(baseline, structural, causal):
        q = a["question"][:42]
        c_total = c["estimated_cost_usd"] + c.get("ingest_cost_usd", 0)
        print(
            f"  {q:<44} {a['total_tokens']:>7,} {b['total_tokens']:>7,} {c['total_tokens']:>7,}"
            f"  ${a['estimated_cost_usd']:>7.5f} ${b['estimated_cost_usd']:>7.5f} ${c_total:>7.5f}"
        )

    c_total_cost = c_cost + c_ing
    print(f"\n  {'TOTAL':<44} {b_tok:>7,} {s_tok:>7,} {c_tok:>7,}"
          f"  ${b_cost:>7.5f} ${s_cost:>7.5f} ${c_total_cost:>7.5f}")
    print(f"  {'TIME (s)':<44} {b_time:>7.1f} {s_time:>7.1f} {c_time:>7.1f}")

    def _pct(a, b): return (a - b) / a * 100 if a else 0

    print(f"\n  Token reduction  vs baseline:  structural={_pct(b_tok, s_tok):+.1f}%   causal={_pct(b_tok, c_tok):+.1f}%")
    print(f"  Token reduction  B vs C:       causal over structural={_pct(s_tok, c_tok):+.1f}%")
    print(f"  Cost reduction   vs baseline:  structural=${b_cost-s_cost:.5f}   causal=${b_cost-c_total_cost:.5f}")
    print(f"  File reads:  A={baseline[0]['file_reads']}  B=0  C=0")
    print(f"\n  Note: C cost includes amortised one-time ingest (${c_ing:.5f}).")
    print(f"        Structural KG (B) has zero ingest cost — edges come from static analysis only.")
    print("=" * 100 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the three-condition memoire benchmark."""
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
    parser.add_argument(
        "--conditions", nargs="+",
        choices=["baseline", "structural", "causal", "all"],
        default=["all"],
        help="Which conditions to run (default: all)",
    )
    args = parser.parse_args()

    root      = Path(args.project_root).resolve()
    questions = args.questions or QUESTIONS
    model     = args.session_model
    run_all   = "all" in args.conditions
    conditions = set(args.conditions)

    if not shutil.which("claude"):
        print("ERROR: 'claude' CLI not found. Install Claude Code to run this benchmark.")
        sys.exit(1)

    baseline_results   = run_baseline(root, questions, model)   if run_all or "baseline"   in conditions else []
    structural_results = run_structural(root, questions, model) if run_all or "structural" in conditions else []
    causal_results     = run_causal(root, questions, model)     if run_all or "causal"     in conditions else []

    print_report(baseline_results, structural_results, causal_results)

    if args.output:
        def _sum(results, key): return sum(r.get(key, 0) for r in results)

        b_tok  = _sum(baseline_results, "total_tokens")
        s_tok  = _sum(structural_results, "total_tokens")
        c_tok  = _sum(causal_results, "total_tokens")
        b_cost = _sum(baseline_results, "estimated_cost_usd")
        s_cost = _sum(structural_results, "estimated_cost_usd")
        c_cost = _sum(causal_results, "estimated_cost_usd")
        c_ing  = _sum(causal_results, "ingest_cost_usd")

        out = {
            "project": str(root),
            "session_model": model,
            "extraction_model": MEMOIRE_EXTRACTION_MODEL,
            "questions": questions,
            "conditions": {
                "baseline":   baseline_results,
                "structural": structural_results,
                "causal":     causal_results,
            },
            "summary": {
                "baseline_tokens":   b_tok,
                "structural_tokens": s_tok,
                "causal_tokens":     c_tok,
                "structural_vs_baseline_pct": round((b_tok - s_tok) / b_tok * 100, 1) if b_tok else 0,
                "causal_vs_baseline_pct":     round((b_tok - c_tok) / b_tok * 100, 1) if b_tok else 0,
                "causal_vs_structural_pct":   round((s_tok - c_tok) / s_tok * 100, 1) if s_tok else 0,
                "baseline_cost_usd":   round(b_cost, 6),
                "structural_cost_usd": round(s_cost, 6),
                "causal_cost_usd":     round(c_cost + c_ing, 6),
                "baseline_file_reads":   baseline_results[0]["file_reads"] if baseline_results else 0,
                "structural_file_reads": 0,
                "causal_file_reads":     0,
            },
        }
        Path(args.output).write_text(json.dumps(out, indent=2))
        print(f"Full results written to {args.output}")


if __name__ == "__main__":
    main()

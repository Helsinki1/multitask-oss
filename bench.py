#!/usr/bin/env python3
"""SWE-bench Lite local evaluation runner.

Usage:
    python bench.py --n 10 --output results.jsonl
    python bench.py --n 1 --max-turns 20          # quick smoke test
    python bench.py --n 10 --model gpt-4o-mini    # cheaper model
"""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from cloud_agent.agent.state import AgentState, BudgetState
from cloud_agent.blueprints.devloop import build_devloop
from cloud_agent.config import settings
from cloud_agent.db.store import StateStore
from cloud_agent.observability.tracer import Tracer


CACHE_DIR = Path.home() / ".swebench_cache" / "repos"
GITHUB_PREFIX = "https://github.com/"


def _repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def _ensure_repo_cached(repo: str) -> Path:
    """Clone or fetch a repo into CACHE_DIR; return the local path."""
    slug = _repo_slug(repo)
    cache_path = CACHE_DIR / slug
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cache_path.exists():
        print(f"  [cache] fetching {repo} ...", flush=True)
        subprocess.run(
            ["git", "-C", str(cache_path), "fetch", "--all", "--quiet"],
            check=True,
        )
    else:
        url = f"{GITHUB_PREFIX}{repo}"
        print(f"  [cache] cloning {repo} (may take a while) ...", flush=True)
        subprocess.run(["git", "clone", "--quiet", url, str(cache_path)], check=True)

    return cache_path


def _prepare_worktree(cache_path: Path, instance_id: str, base_commit: str) -> str:
    """Create an isolated git worktree at base_commit; return tmpdir path."""
    tmpdir = tempfile.mkdtemp(prefix=f"swebench_{instance_id}_")
    branch_name = f"swebench/{instance_id}"
    subprocess.run(
        ["git", "-C", str(cache_path), "worktree", "add",
         "--quiet", "-b", branch_name, tmpdir, base_commit],
        check=True,
    )
    return tmpdir


def _remove_worktree(cache_path: Path, tmpdir: str) -> None:
    subprocess.run(
        ["git", "-C", str(cache_path), "worktree", "remove", "--force", tmpdir],
        check=False,
    )
    if os.path.exists(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)


def _extract_patch(tmpdir: str, base_commit: str) -> str:
    """Return unified diff of everything the agent changed relative to base_commit."""
    result = subprocess.run(
        ["git", "-C", tmpdir, "diff", base_commit, "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _parse_test_list(raw) -> list[str]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [raw]
    return []


def _build_task_text(instance: dict) -> str:
    repo = instance["repo"]
    problem = instance["problem_statement"].strip()
    fail_tests = _parse_test_list(instance.get("FAIL_TO_PASS", []))

    lines = [f"Fix the following GitHub issue in {repo}:", "", problem]
    if fail_tests:
        lines += ["", "After your fix, the following tests must pass:"]
        lines += [f"  {t}" for t in fail_tests[:10]]
    return "\n".join(lines)


def _run_instance(instance: dict, args: argparse.Namespace) -> dict:
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]

    print(f"\n{'='*60}")
    print(f"Instance: {instance_id}")
    print(f"Repo:     {repo}  @ {base_commit[:8]}")

    try:
        cache_path = _ensure_repo_cached(repo)
        tmpdir = _prepare_worktree(cache_path, instance_id, base_commit)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR (setup): {exc}")
        return {
            "instance_id": instance_id,
            "model_patch": "",
            "model_name_or_path": "cloud-agent",
            "status": "error",
            "error": f"setup failed: {exc}",
        }

    try:
        task_text = _build_task_text(instance)

        if args.model:
            settings.implement_model = args.model

        state = AgentState(
            workspace_path=tmpdir,
            task_text=task_text,
            task_source="swebench",
            budgets=BudgetState(
                max_llm_turns=args.max_turns,
                max_cost_usd=args.max_cost,
            ),
        )

        state_store = StateStore()
        tracer = Tracer(session_id=state.session_id, traces_dir=settings.traces_dir)
        print(f"Session: {state.session_id}  Trace: {tracer.path}")

        engine = build_devloop(tracer=tracer, state_store=state_store)
        final = engine.run(state)

        patch = _extract_patch(tmpdir, base_commit)

        print(f"Status:  {final.task_status}  Turns: {final.budgets.used_llm_turns}  "
              f"Cost: ${final.budgets.used_cost_usd:.3f}  Patch: {len(patch)} chars")

        return {
            "instance_id": instance_id,
            "model_patch": patch,
            "model_name_or_path": "cloud-agent",
            "status": final.task_status,
            "turns": final.budgets.used_llm_turns,
            "cost_usd": round(final.budgets.used_cost_usd, 4),
        }

    except Exception as exc:
        print(f"ERROR (agent): {exc}")
        return {
            "instance_id": instance_id,
            "model_patch": "",
            "model_name_or_path": "cloud-agent",
            "status": "error",
            "error": str(exc),
        }
    finally:
        _remove_worktree(cache_path, tmpdir)


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench Lite local evaluation runner")
    parser.add_argument("--n", type=int, default=10,
                        help="Number of instances to run (default: 10)")
    parser.add_argument("--output", default="results.jsonl",
                        help="Output JSONL file (default: results.jsonl)")
    parser.add_argument("--model", default=None,
                        help="Override implement model (e.g. gpt-4o, gpt-4o-mini)")
    parser.add_argument("--max-turns", type=int, default=50,
                        help="Max LLM turns per instance (default: 50)")
    parser.add_argument("--max-cost", type=float, default=3.0,
                        help="Max cost per instance in USD (default: $3)")
    args = parser.parse_args()

    if not settings.openai_api_key:
        print("Error: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    try:
        from datasets import load_dataset
    except ImportError:
        print("Error: 'datasets' not installed. Run: pip install -e .", file=sys.stderr)
        sys.exit(1)

    print("Loading SWE-bench Lite dataset...")
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    n = min(args.n, len(ds))
    instances = list(ds.select(range(n)))

    print(f"Running {n} instances → {args.output}")
    print(f"Model: {args.model or settings.implement_model}  "
          f"Max turns: {args.max_turns}  Max cost/instance: ${args.max_cost}")

    results: list[dict] = []
    for i, instance in enumerate(instances, 1):
        print(f"\n[{i}/{n}]", end="")
        result = _run_instance(instance, args)
        results.append(result)

        with open(args.output, "a") as f:
            f.write(json.dumps({
                "instance_id": result["instance_id"],
                "model_patch": result["model_patch"],
                "model_name_or_path": result["model_name_or_path"],
            }) + "\n")

    resolved = sum(1 for r in results if r.get("model_patch"))
    total_cost = sum(r.get("cost_usd", 0) for r in results)

    print(f"\n\n{'='*60}")
    print(f"SUMMARY — {n} instances")
    print(f"{'='*60}")
    print(f"Patches produced: {resolved}/{n}")
    print(f"Total cost:       ${total_cost:.3f}")
    print(f"Results saved to: {args.output}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

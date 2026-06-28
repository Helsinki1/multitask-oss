#!/usr/bin/env python3
"""SWE-bench evaluation harness for cloud_agent.

Handles everything the agent should NOT have to think about:
  - Cloning the repo at the correct base commit
  - Applying the test_patch (the failing tests that prove the bug exists)
  - Installing the repo environment (best-effort)

Then runs cloud_agent in eval_mode (no mirror-building, deterministic repro/verify
against the planted FAIL_TO_PASS test IDs), and evaluates the result.

Usage:
  # by instance ID (requires: pip install datasets)
  python3 swe_bench_run.py --instance-id django__django-12345

  # by dataset row index
  python3 swe_bench_run.py --index 0 [--dataset princeton-nlp/SWE-bench_Lite] [--split test]

  # from a local JSON file (single instance dict)
  python3 swe_bench_run.py --instance-file path/to/instance.json

Optional flags:
  --work-dir DIR        Use DIR as workspace (must be empty/nonexistent). Default: temp dir.
  --keep-workspace      Do not delete the workspace after the run (useful for debugging).
  --max-turns N         Max LLM turns for the agent (default: 30).
  --max-cost DOLLARS    Max spend in USD (default: 2.00).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# ── Instance loading ──────────────────────────────────────────────────────────

def load_instance(
    *,
    instance_id: str | None = None,
    index: int | None = None,
    instance_file: str | None = None,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    split: str = "test",
) -> dict:
    if instance_file:
        return json.loads(Path(instance_file).read_text())

    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: 'datasets' not installed. Run: pip install datasets", file=sys.stderr)
        sys.exit(1)

    ds = load_dataset(dataset, split=split)

    if instance_id is not None:
        hits = [i for i, row in enumerate(ds) if row["instance_id"] == instance_id]
        if not hits:
            print(f"ERROR: instance {instance_id!r} not found in {dataset}/{split}", file=sys.stderr)
            sys.exit(1)
        return dict(ds[hits[0]])

    if index is not None:
        return dict(ds[index])

    raise ValueError("Provide --instance-id, --index, or --instance-file")


def _parse_test_ids(raw: list | str) -> list[str]:
    """FAIL_TO_PASS / PASS_TO_PASS can be a JSON string or already a list."""
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


# ── Workspace setup ───────────────────────────────────────────────────────────

def setup_workspace(instance: dict, work_dir: str) -> None:
    """Clone repo at base_commit, apply test_patch, install the environment."""
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    test_patch = instance["test_patch"]

    print(f"  cloning https://github.com/{repo}.git ...")
    subprocess.run(
        ["git", "clone", f"https://github.com/{repo}.git", work_dir],
        check=True,
    )
    subprocess.run(["git", "checkout", base_commit], cwd=work_dir, check=True)

    if test_patch.strip():
        patch_path = Path(work_dir) / "_test_patch.diff"
        patch_path.write_text(test_patch)
        try:
            subprocess.run(
                ["git", "apply", "--allow-empty", str(patch_path)],
                cwd=work_dir,
                check=True,
            )
        finally:
            patch_path.unlink(missing_ok=True)
        # Commit so the workspace is clean when the agent's CheckBranchNode runs
        subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
        subprocess.run(
            [
                "git", "-c", "user.email=harness@swe-bench",
                "-c", "user.name=SWE-bench Harness",
                "commit", "-m", "test_patch",
            ],
            cwd=work_dir,
            check=True,
        )
        print("  applied and committed test_patch")

    _install_env(work_dir)


def _install_env(work_dir: str) -> None:
    """Best-effort: install the repo and any dev/test requirements."""
    w = Path(work_dir)

    if (w / "pyproject.toml").exists() or (w / "setup.py").exists() or (w / "setup.cfg").exists():
        print("  pip install -e . ...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
            cwd=work_dir,
            check=False,
        )

    for req in ("requirements-dev.txt", "requirements-test.txt", "requirements.txt"):
        if (w / req).exists():
            print(f"  pip install -r {req} ...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req, "--quiet"],
                cwd=work_dir,
                check=False,
            )
            break


# ── Agent run ─────────────────────────────────────────────────────────────────

def run_agent(instance: dict, work_dir: str, max_turns: int, max_cost: float):
    """Build eval_mode AgentState and run the devloop engine."""
    from agent.context import resolve_test_ids
    from agent.state import AgentState, BudgetState
    from blueprints.devloop import build_devloop
    from cloud_agent.config import settings
    from db.store import StateStore
    from observability.tracer import Tracer

    fail_to_pass = _parse_test_ids(instance["FAIL_TO_PASS"])
    pass_to_pass = _parse_test_ids(instance["PASS_TO_PASS"])

    # Resolve bare / Django-style test IDs to full pytest node IDs now,
    # so every downstream consumer (prompts, VERIFY, GATHER_CONTEXT) sees
    # runnable test identifiers.
    # Pass ALL test IDs together so majority-vote file detection has the
    # strongest possible signal (22 IDs beat 1).
    all_resolved = resolve_test_ids(work_dir, fail_to_pass + pass_to_pass)
    fail_to_pass = all_resolved[:len(fail_to_pass)]
    pass_to_pass = all_resolved[len(fail_to_pass):]

    task = (
        f"{instance['problem_statement'].strip()}\n\n"
        f"Tests that must pass after your fix: {' '.join(fail_to_pass)}"
    )

    state = AgentState(
        workspace_path=work_dir,
        task_text=task,
        task_source="swe_bench",
        eval_mode=True,
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        budgets=BudgetState(max_llm_turns=max_turns, max_cost_usd=max_cost),
    )

    tracer = Tracer(session_id=state.session_id, traces_dir=settings.traces_dir)
    engine = build_devloop(tracer=tracer, state_store=StateStore())
    return engine.run(state)


# ── Final evaluation ──────────────────────────────────────────────────────────

def evaluate(instance: dict, work_dir: str) -> tuple[bool, dict[str, bool]]:
    """Independent post-run check: FAIL_TO_PASS must pass, PASS_TO_PASS must not regress."""
    from agent.context import resolve_test_ids

    raw_f2p = _parse_test_ids(instance["FAIL_TO_PASS"])
    raw_p2p = _parse_test_ids(instance["PASS_TO_PASS"])
    all_resolved = resolve_test_ids(work_dir, raw_f2p + raw_p2p)
    fail_to_pass = all_resolved[:len(raw_f2p)]
    pass_to_pass = all_resolved[len(raw_f2p):]

    def _pytest(test_ids: list[str], stop_on_first: bool = True) -> bool:
        flags = ["-x", "--tb=short", "-q"] if stop_on_first else ["--tb=short", "-q"]
        r = subprocess.run(
            [sys.executable, "-m", "pytest", *test_ids, *flags],
            cwd=work_dir,
            capture_output=True,
        )
        return r.returncode == 0

    results: dict[str, bool] = {}
    if fail_to_pass:
        results["fail_to_pass"] = _pytest(fail_to_pass, stop_on_first=True)
    if pass_to_pass:
        results["pass_to_pass"] = _pytest(pass_to_pass, stop_on_first=False)

    return all(results.values()), results


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SWE-bench harness: clone → checkout → test_patch → env install → agent → eval"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--instance-id", metavar="ID", help="SWE-bench instance_id string")
    src.add_argument("--instance-file", metavar="FILE", help="Path to instance JSON file")
    src.add_argument("--index", type=int, metavar="N", help="Dataset row index")
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite", metavar="DATASET")
    parser.add_argument("--split", default="test", metavar="SPLIT")
    parser.add_argument(
        "--work-dir", metavar="DIR",
        help="Workspace path (empty/nonexistent directory). Default: temp dir.",
    )
    parser.add_argument(
        "--keep-workspace", action="store_true",
        help="Do not delete workspace after run (useful for debugging).",
    )
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-cost", type=float, default=2.0)
    args = parser.parse_args()

    instance = load_instance(
        instance_id=args.instance_id,
        index=args.index,
        instance_file=args.instance_file,
        dataset=args.dataset,
        split=args.split,
    )

    fail_to_pass = _parse_test_ids(instance["FAIL_TO_PASS"])
    pass_to_pass = _parse_test_ids(instance["PASS_TO_PASS"])

    print(f"\nInstance : {instance['instance_id']}")
    print(f"Repo     : {instance['repo']}  @{instance['base_commit'][:8]}")
    print(f"F→P      : {fail_to_pass}")
    print(f"P→P      : {len(pass_to_pass)} tests")

    tmp = None
    if args.work_dir:
        work_dir = args.work_dir
        p = Path(work_dir)
        if p.exists() and any(p.iterdir()):
            print(f"ERROR: --work-dir {work_dir!r} is not empty.", file=sys.stderr)
            return 1
        p.mkdir(parents=True, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory()
        work_dir = tmp.name

    try:
        print(f"\n[setup] {work_dir}")
        setup_workspace(instance, work_dir)

        print("\n[agent]")
        final = run_agent(instance, work_dir, args.max_turns, args.max_cost)
        print(f"  status={final.task_status}  "
              f"cost=${final.budgets.used_cost_usd:.4f}  "
              f"turns={final.budgets.used_llm_turns}")

        print("\n[eval]")
        passed, results = evaluate(instance, work_dir)
        for label, ok in results.items():
            print(f"  {'✓' if ok else '✗'} {label}")
        print(f"\n{'PASS' if passed else 'FAIL'}  {instance['instance_id']}")
        return 0 if passed else 1

    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    finally:
        if tmp is not None and not args.keep_workspace:
            tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())

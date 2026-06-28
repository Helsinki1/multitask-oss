#!/usr/bin/env python3
"""Run swe_bench_run.py on Modal — beefy remote Linux, your API keys, your harness.

Setup (one-time):
  pip install modal
  modal setup
  modal secret create openai-keys OPENAI_API_KEY=sk-...

Run one instance:
  modal run modal_run.py::run_one --instance-id django__django-12345

Run many in parallel (reads instance IDs from a file, one per line):
  modal run modal_run.py::run_batch --ids-file ids.txt

Results are printed to stdout; set RESULTS_DIR below to also write JSON files locally.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import modal

# ── Image: Ubuntu + git + your harness deps ───────────────────────────────────

HARNESS_DIR = Path(__file__).parent  # local repo root

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(["git", "gcc", "python3-dev"])
    .pip_install([
        "openai>=1.0",
        "pydantic>=2.0",
        "python-dotenv>=1.0",
        "datasets",
        "pytest",
    ])
    .run_commands(
        "git config --global user.email 'agent@swe-bench'",
        "git config --global user.name 'SWE-bench Agent'",
    )
    .add_local_dir(
        str(HARNESS_DIR),
        remote_path="/app",
        ignore=[".git", "__pycache__", ".pytest_cache", "traces", "build", "*.db"],
    )
)

app = modal.App("swe-bench-harness")

# ── Core function ─────────────────────────────────────────────────────────────

@app.function(
    image=image,
    secrets=[modal.Secret.from_name("openai-keys")],
    cpu=4,
    memory=8192,  # 8 GB — plenty for git clone + pytest; bump to 16384 if needed
    timeout=60 * 60,  # 1 hour per instance
)
def run_instance(
    instance_id: str | None = None,
    index: int | None = None,
    instance_json: str | None = None,  # pre-serialized instance dict
    max_turns: int = 60,
    max_cost: float = 50.0,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    split: str = "test",
) -> dict:
    """Runs one SWE-bench instance end-to-end and returns a result dict."""
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, "/app")
    os.chdir("/app")

    # Modal mounts are read-only; redirect writable state to /tmp
    os.environ.setdefault("DB_PATH", "/tmp/cloud_agent.db")
    os.environ.setdefault("TRACES_DIR", "/tmp/traces")
    Path("/tmp/traces").mkdir(exist_ok=True)

    from swe_bench_run import evaluate, load_instance, run_agent, setup_workspace

    # ── Load instance ──────────────────────────────────────────────────────
    if instance_json:
        instance = json.loads(instance_json)
    else:
        instance = load_instance(
            instance_id=instance_id,
            index=index,
            dataset=dataset,
            split=split,
        )

    iid = instance["instance_id"]
    print(f"\n=== {iid} ===")
    print(f"repo: {instance['repo']}  @{instance['base_commit'][:8]}")

    with tempfile.TemporaryDirectory() as work_dir:
        # ── Setup ──────────────────────────────────────────────────────────
        try:
            setup_workspace(instance, work_dir)
        except Exception as exc:
            return {"instance_id": iid, "outcome": "setup_error", "error": str(exc)}

        # ── Agent ──────────────────────────────────────────────────────────
        try:
            final = run_agent(instance, work_dir, max_turns, max_cost)
            agent_meta = {
                "status": final.task_status,
                "turns": final.budgets.used_llm_turns,
                "cost_usd": round(final.budgets.used_cost_usd, 4),
            }
        except Exception as exc:
            return {"instance_id": iid, "outcome": "agent_error", "error": str(exc)}

        # ── Eval ───────────────────────────────────────────────────────────
        try:
            passed, results = evaluate(instance, work_dir)
        except Exception as exc:
            return {
                "instance_id": iid,
                "outcome": "eval_error",
                "error": str(exc),
                **agent_meta,
            }

    outcome = "pass" if passed else "fail"
    print(f"{outcome.upper()}  {iid}  cost=${agent_meta['cost_usd']}  turns={agent_meta['turns']}")

    return {
        "instance_id": iid,
        "outcome": outcome,
        "test_results": results,
        **agent_meta,
    }


# ── Local entrypoints ─────────────────────────────────────────────────────────

@app.local_entrypoint()
def run_one(
    instance_id: str = "",
    index: int = -1,
    max_turns: int = 60,
    max_cost: float = 50.0,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    split: str = "test",
) -> None:
    """modal run modal_run.py::run_one --instance-id django__django-12345"""
    kwargs: dict = {"max_turns": max_turns, "max_cost": max_cost, "dataset": dataset, "split": split}
    if instance_id:
        kwargs["instance_id"] = instance_id
    elif index >= 0:
        kwargs["index"] = index
    else:
        print("ERROR: pass --instance-id or --index", file=sys.stderr)
        sys.exit(1)

    result = run_instance.remote(**kwargs)
    print(json.dumps(result, indent=2))


@app.local_entrypoint()
def run_repo(
    repo: str = "sympy/sympy",
    limit: int = 10,
    max_turns: int = 60,
    max_cost: float = 50.0,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    split: str = "test",
    results_file: str = "results.jsonl",
) -> None:
    """Run the first --limit instances from a specific repo in parallel on Modal.

    modal run modal_run.py::run_repo --repo sympy/sympy --limit 10
    """
    from datasets import load_dataset

    ds = load_dataset(dataset, split=split)
    ids = [row["instance_id"] for row in ds if row["repo"] == repo][:limit]
    if not ids:
        print(f"ERROR: no instances found for repo {repo!r}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(ids)} instances for {repo}: {ids}")

    static_kwargs = {"max_turns": max_turns, "max_cost": max_cost, "dataset": dataset, "split": split}

    out = Path(results_file)
    passed = failed = errors = 0

    with out.open("w") as f:
        for result in run_instance.map(ids, kwargs=static_kwargs, order_outputs=False):
            f.write(json.dumps(result) + "\n")
            f.flush()
            o = result.get("outcome", "?")
            if o == "pass":
                passed += 1
            elif o == "fail":
                failed += 1
            else:
                errors += 1
            print(f"  [{passed+failed+errors}/{len(ids)}] {result['instance_id']} → {o}")

    print(f"\nDone. pass={passed} fail={failed} errors={errors}")
    print(f"Results written to {out}")


@app.local_entrypoint()
def run_batch(
    ids_file: str = "ids.txt",
    max_turns: int = 60,
    max_cost: float = 50.0,
    dataset: str = "princeton-nlp/SWE-bench_Lite",
    split: str = "test",
    results_file: str = "results.jsonl",
) -> None:
    """Run all instance IDs in ids_file in parallel on Modal.

    ids.txt format: one instance_id per line, blank lines / # comments ignored.

    modal run modal_run.py::run_batch --ids-file ids.txt --results-file results.jsonl
    """
    ids = [
        line.strip()
        for line in Path(ids_file).read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not ids:
        print("ERROR: ids_file is empty", file=sys.stderr)
        sys.exit(1)

    print(f"Submitting {len(ids)} instances in parallel...")

    static_kwargs = {"max_turns": max_turns, "max_cost": max_cost, "dataset": dataset, "split": split}

    out = Path(results_file)
    passed = failed = errors = 0

    with out.open("w") as f:
        for result in run_instance.map(ids, kwargs=static_kwargs, order_outputs=False):
            f.write(json.dumps(result) + "\n")
            f.flush()
            o = result.get("outcome", "?")
            if o == "pass":
                passed += 1
            elif o == "fail":
                failed += 1
            else:
                errors += 1
            print(f"  [{passed+failed+errors}/{len(ids)}] {result['instance_id']} → {o}")

    print(f"\nDone. pass={passed} fail={failed} errors={errors}")
    print(f"Results written to {out}")

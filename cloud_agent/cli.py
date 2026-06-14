"""CLI entry point for cloud_agent."""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from cloud_agent.agent.state import AgentState, BudgetState
from cloud_agent.blueprints.devloop import build_devloop
from cloud_agent.config import settings
from cloud_agent.db.store import StateStore
from cloud_agent.observability.tracer import Tracer


def main() -> None:
    parser = argparse.ArgumentParser(description="cloud_agent — Phase 1 coding agent")
    parser.add_argument("--repo", required=True, help="Path to the repository")
    parser.add_argument("--task", required=True, help="Task description for the agent")
    parser.add_argument("--max-turns", type=int, default=100, help="Max LLM turns (default: 100)")
    parser.add_argument("--max-cost", type=float, default=5.0, help="Max cost in USD (default: $5)")
    parser.add_argument("--model", default=None, help="Override implement model")
    parser.add_argument(
        "--reset-repo",
        action="store_true",
        help="Reset the repo to a clean state before running (git reset --hard + clean)",
    )
    args = parser.parse_args()

    if not settings.openai_api_key:
        print("Error: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    repo_path = os.path.realpath(args.repo)
    if not os.path.isdir(repo_path):
        print(f"Error: repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    if args.reset_repo:
        import subprocess
        print(f"Resetting repo at {repo_path}...")
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path, check=True)
        subprocess.run(["git", "clean", "-fd"], cwd=repo_path, check=True)

    if args.model:
        settings.implement_model = args.model

    state = AgentState(
        workspace_path=repo_path,
        task_text=args.task,
        task_source="cli",
        budgets=BudgetState(
            max_llm_turns=args.max_turns,
            max_cost_usd=args.max_cost,
        ),
    )

    state_store = StateStore()
    tracer = Tracer(session_id=state.session_id, traces_dir=settings.traces_dir)

    print(f"\nTask:    {args.task}")
    print(f"Repo:    {repo_path}")
    print(f"Session: {state.session_id}")
    print(f"Trace:   {tracer.path}")
    print(f"Model:   {settings.implement_model}")
    print()

    engine = build_devloop(tracer=tracer, state_store=state_store)
    final = engine.run(state)

    print()
    print("=" * 60)
    print(f"Status:  {final.task_status}")
    print(f"Branch:  {final.working_branch or '(unchanged)'}")
    print(f"Cost:    ${final.budgets.used_cost_usd:.4f}")
    print(f"Turns:   {final.budgets.used_llm_turns}")
    print(f"Tools:   {final.budgets.used_tool_calls}")
    if final.errors:
        print(f"Errors:  {len(final.errors)}")
        for e in final.errors:
            print(f"  [{e.node}] {e.message}")
    print(f"Trace:   {tracer.path}")
    print("=" * 60)

    sys.exit(0 if final.task_status in ("done", "running") else 1)


if __name__ == "__main__":
    main()

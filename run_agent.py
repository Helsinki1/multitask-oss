#!/usr/bin/env python3
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from agent.graph import build_graph  # noqa: E402 (after load_dotenv)


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal coding agent")
    parser.add_argument("--repo", required=True, help="Path to the repo to fix")
    parser.add_argument("--task", required=True, help="Task description for the agent")
    parser.add_argument("--max-iterations", type=int, default=5, help="Max patch/test iterations")
    args = parser.parse_args()

    repo_path = os.path.realpath(args.repo)
    if not os.path.isdir(repo_path):
        print(f"Error: repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    initial_state = {
        "task": args.task,
        "repo_path": repo_path,
        "iteration": 0,
        "max_iterations": args.max_iterations,
        "file_listing": [],
        "files_read": {},
        "plan": "",
        "patches_attempted": [],
        "patch_errors": [],
        "test_runs": [],
        "test_passed": False,
        "messages": [],
        "summary": "",
        "done": False,
    }

    print(f"Starting agent on repo: {repo_path}")
    print(f"Task: {args.task}\n")

    graph = build_graph()
    final_state = graph.invoke(initial_state)

    print(final_state.get("summary", "No summary produced."))
    print(f"\nTrace log: traces/actions.jsonl")


if __name__ == "__main__":
    main()

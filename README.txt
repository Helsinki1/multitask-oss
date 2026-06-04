Ready for review
Select text to add comments on the plan
Plan: Minimal Coding Agent (v0)
Context
Build a small Python coding agent that can autonomously fix a broken repo by reading files, generating patches, running tests in a Docker sandbox, observing output, and iterating. The goal is a working end-to-end demo: give it a tiny broken Python repo + task string, and it patches it until tests pass or it hits 5 iterations. No browser, no VM, no UI yet.

1. Project Folder Structure
multitask-oss/
├── agent/
│   ├── __init__.py
│   ├── state.py          # AgentState TypedDict
│   ├── graph.py          # LangGraph graph + edges
│   ├── nodes.py          # Node functions (one per step)
│   ├── tools.py          # Filesystem tools (list, read, patch)
│   └── docker_runner.py  # Docker SDK wrapper
├── traces/               # JSONL action log (gitignored)
├── demo_repo/            # Tiny broken Python repo
│   ├── math_utils.py     # Has intentional bugs
│   └── test_math_utils.py
├── run_agent.py          # CLI entrypoint
├── pyproject.toml
├── .env.example
└── .gitignore
2. Agent State Schema (agent/state.py)
class AgentState(TypedDict):
    task: str                        # e.g. "fix the failing tests"
    repo_path: str                   # absolute path on host
    iteration: int                   # current loop count
    max_iterations: int              # hard cap = 5
    file_listing: list[str]          # relative paths from list_files()
    files_read: dict[str, str]       # rel_path -> content
    plan: str                        # LLM-generated short plan
    patches_attempted: list[str]     # unified diff strings
    patch_errors: list[str]          # git apply stderr per attempt
    test_runs: list[dict]            # [{stdout, stderr, exit_code}, ...]
    test_passed: bool
    messages: list                   # LangChain message objects for LLM
    summary: str                     # final human-readable result
    done: bool
3. LangGraph Node Design (agent/nodes.py + agent/graph.py)
Nodes (one Python function each)
Node	Responsibility
list_files	Walk repo_path, populate file_listing
read_files	LLM picks which files to read; reads them into files_read
plan_node	LLM generates a short bulleted plan given task + file contents
generate_patch	LLM produces a unified diff targeting the identified bug(s)
apply_patch	Calls git apply; records result in patch_errors
run_tests	Runs pytest in Docker; appends to test_runs
analyze	LLM reads test output; sets test_passed or increments iteration
summarize	Formats final summary from all trace data; sets done=True
Graph edges
START
  → list_files
  → read_files
  → plan_node
  → generate_patch
  → apply_patch
  → run_tests
  → analyze
    ┌─ test_passed=True  → summarize → END
    └─ iteration >= max  → summarize → END
    └─ otherwise         → generate_patch  (loop)
Conditional edge on analyze: check state["test_passed"] or state["iteration"] >= state["max_iterations"].

4. Tool Interfaces (agent/tools.py)
All tools enforce the repo_path safety boundary (no .. escapes).

def list_files(repo_path: str) -> list[str]:
    # os.walk, return relative paths, skip .git

def read_file(repo_path: str, rel_path: str) -> str:
    # resolve + check startswith(repo_path), read text

def write_file(repo_path: str, rel_path: str, content: str) -> None:
    # same safety check, write text (used for test fixtures if needed)

def apply_patch(repo_path: str, patch_text: str) -> tuple[bool, str]:
    # write patch to tempfile, run git apply --whitespace=fix,
    # return (success, stderr)
5. Docker Runner (agent/docker_runner.py)
import docker

def run_in_docker(repo_path: str, command: str) -> dict:
    # Mount repo_path → /workspace (rw)
    # Image: python:3.12-slim
    # network_mode="none" after pip install (or "bridge" for install, then "none")
    # Returns {stdout, stderr, exit_code}
Strategy for pip deps: if a requirements.txt exists in the repo, run pip install -r requirements.txt -q && pytest as a single shell command so network is only needed for that one container run. After packages are in the layer, switch to network_mode="none" on subsequent runs (or accept bridge for v0 simplicity).

6. Trace Logger (agent/state.py or inline in nodes)
Every action/observation appended to traces/actions.jsonl:

{"ts": "2026-06-04T12:00:00Z", "iteration": 1, "event": "apply_patch", "patch": "...", "success": true}
{"ts": "...", "iteration": 1, "event": "run_tests", "exit_code": 1, "stdout": "..."}
Single helper:

def log_event(event: str, **kwargs) -> None:
    # append JSON line to traces/actions.jsonl
7. Safety Boundaries
Path traversal: every file read/write resolves the full path and asserts it starts with repo_path. Raise ValueError otherwise.
Docker isolation: tests run inside a container; host filesystem is not exposed beyond the repo volume mount.
No shell injection: all Docker commands passed as list args, not shell strings. git apply called via subprocess.run(["git", "apply", ...], ...).
Iteration cap: max_iterations=5 hardcoded in run_agent.py; the graph's conditional edge enforces it.
No network in tests: network_mode="none" on the pytest container run (pip install gets bridge in a separate step if needed).
8. Demo Repo (demo_repo/)
demo_repo/math_utils.py — intentional bugs:

def add(a, b):
    return a - b   # bug: should be +

def multiply(a, b):
    return a + b   # bug: should be *
demo_repo/test_math_utils.py:

from math_utils import add, multiply

def test_add():
    assert add(2, 3) == 5

def test_multiply():
    assert multiply(3, 4) == 12
The agent's task string: "Fix the failing pytest tests in this repo."

Expected agent behavior:

Lists files → sees math_utils.py, test_math_utils.py
Reads both
Plans: "fix add to use +, fix multiply to use *"
Generates unified diff for both functions
Applies patch via git apply
Runs pytest in Docker → passes
Returns summary
9. Minimal Dependencies (pyproject.toml)
[project]
name = "velvety-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "langgraph>=0.2",
    "langchain-anthropic>=0.3",
    "docker>=7.0",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest", "ruff"]
LLM: Claude via langchain-anthropic (uses ANTHROPIC_API_KEY from .env).

10. Step-by-Step Build Order (incremental commits)
Commit	What to build	Files
1	Scaffold	pyproject.toml, .gitignore, agent/__init__.py, .env.example
2	State + tracer	agent/state.py
3	Filesystem tools	agent/tools.py (list, read, apply_patch)
4	Docker runner	agent/docker_runner.py
5	Nodes (non-LLM)	list_files, apply_patch, run_tests nodes in agent/nodes.py
6	Nodes (LLM)	read_files, plan_node, generate_patch, analyze, summarize
7	Graph wiring	agent/graph.py (compile graph, conditional edges)
8	CLI entrypoint	run_agent.py
9	Demo repo	demo_repo/math_utils.py, demo_repo/test_math_utils.py
10	End-to-end smoke test	Run python run_agent.py --repo demo_repo --task "Fix the failing tests"
11. Files to Create First
pyproject.toml — establishes the package and deps
.gitignore — exclude traces/, .env, __pycache__, .venv
agent/state.py — the TypedDict is the contract everything else builds against
agent/tools.py — pure functions, testable without Docker or LLM
agent/docker_runner.py — isolated wrapper
agent/nodes.py — imports tools + docker_runner, calls LLM
agent/graph.py — wires nodes into LangGraph StateGraph
run_agent.py — thin CLI (argparse)
demo_repo/ — the broken repo
Verification
End-to-end test (manual, commit 10):

# Terminal 1 – confirm Docker is up
docker info

# Terminal 2
cd /home/davidx/Downloads/multitask-oss
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env  # add ANTHROPIC_API_KEY
python run_agent.py --repo demo_repo --task "Fix the failing tests"
Expected: agent iterates ≤ 5 times, test_passed=True, traces/actions.jsonl has entries for every node, final summary printed to stdout.

Unit verification (commit 4):

# Verify tools.py with no Docker/LLM
python -c "from agent.tools import list_files; print(list_files('demo_repo'))"
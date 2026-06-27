# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable)
pip install -e ".[dev]"

# Run tests
pytest tests/ -x -q

# Lint / format
ruff check . && ruff format .

# Type check
mypy agent/ blueprints/ tools/

# Run one SWE-bench instance locally
python swe_bench_run.py --instance-id django__django-12345 --max-turns 30 --max-cost 2.0

# Run one instance on Modal
modal run modal_run.py::run_one --instance-id django__django-12345

# Run a batch on Modal (parallel)
modal run modal_run.py::run_batch --ids-file ids.txt --results-file results.jsonl

# Run the CLI agent (local repo)
python run_agent.py
```

## Architecture

### State machine (BlueprintEngine)

`agent/runtime.py` drives a node graph: each `Node.run()` returns a `NodeResult(next_node, state_update, status)`. The engine loops until `next_node == "END"`, persisting `AgentState` to SQLite after every node. Exceptions route to `node.failure_next`.

**Bug fix path** (`blueprints/devloop.py`):
```
CHECK_BRANCH → LOAD_TASK → GATHER_CONTEXT → IMPLEMENT → VERIFY
                                ↑ (p2p regression)         |
                                └──────────────────────────┤
                          IMPLEMENT ←── (f2p still failing)┤
                                              CHECKPOINT ←─┘ (all pass or max retries)
```

**Additive path**:
```
CHECK_BRANCH → LOAD_TASK → GATHER_ADDITIVE_CONTEXT → DEFINE_CONTRACT → IMPLEMENT
                                                             ↑               |
                                                       (still failing)       ↓
                                                          VERIFY_ADDITIVE → CHECKPOINT
```

`VERIFY` has `MAX_VERIFY_ATTEMPTS=3`. Crucially, it routes differently by failure type: **f2p failure → IMPLEMENT** (fix is wrong), **p2p regression → GATHER_CONTEXT** (fix broke something, need new context from regression traceback).

### AgentState (`agent/state.py`)

Frozen-copy dataclass (`.apply_update()` returns a new instance). Key fields:

- `todo_list: TestToDoList` — structured record of each test's current status and traceback, updated deterministically by `GATHER_CONTEXT` and `VERIFY`. Never LLM-populated.
- `verify_failure_type` — `"f2p_failing"` | `"p2p_regression"` | `""` — drives the branching logic in `GATHER_CONTEXT` and prompt assembly.
- `verify_attempts` — incremented by `VERIFY` on each failed loop.
- `eval_mode / fail_to_pass / pass_to_pass` — SWE-bench mode.
- `task_type` — `"bug_fix"` | `"additive"` — set by `LOAD_TASK`.
- `context_bundle: ContextBundle` — pre-loaded files from traceback/dep-graph context gathering.
- `contract_test_path` — additive path only; path to `_contract_tests.py`.

### TestToDoList (`agent/state.py`)

Owns the structured test status. Properties: `.f2p_failing`, `.p2p_failing`, `.all_f2p_pass`, `.all_pass`, `.summary()`. Each `TestCase` has `test_id`, `category`, `status`, `traceback`.

### Context gathering (`agent/context.py`)

**Bug fix** — `build_bugfix_context()`: runs failing tests with pytest, parses tracebacks via `_parse_traceback()` (two formats: standard Python and pytest short), follows import graph one hop via `_build_import_subgraph()` (AST, no execution). Returns `TestToDoList` and `ContextBundle` with causally-adjacent source files.

**Regression re-entry** — `rebuild_context_from_regressions()`: called by `GATHER_CONTEXT` when `verify_failure_type == "p2p_regression"`. Parses tracebacks already stored in the `TestToDoList`.

**Additive** — `build_additive_context()`: builds full import graph across all workspace Python files, scores each file by in-degree (how many other files import it) + keyword match with task text. No LLM involved.

### LLM subsession (`agent/subsession.py`)

Stripped-down ReAct loop:
1. Call model, append assistant message
2. If `finish_reason == "tool_calls"`: execute tools, append results, loop
3. Otherwise (text stop): return `status="done"` immediately

**No completion checker. No self-assessment. No nudging.** Correctness is verified deterministically by `VERIFY`. History compression fires at turn 20 (keeps last 6 turns of tool output).

### Prompt assembly (`agent/prompts.py`)

`build_bugfix_system()` composes: base rules + env + tools + repo rules + objective layer. The objective layer varies by retry state:
- First attempt: `_BUGFIX_OBJECTIVE` with all f2p tests and tracebacks
- f2p retry: `_BUGFIX_RETRY_F2P` with still-failing tests, tracebacks, and current git diff
- p2p retry: `_BUGFIX_RETRY_P2P` with regression tests, tracebacks, and current git diff

`build_implement_human()` renders the pre-loaded context files from `ContextBundle.task_adjacent_files` (capped at 14k chars total).

### IMPLEMENT node (`blueprints/nodes/implement.py`)

Seeds `_agent_scripts/` with `mro_check.py` and `import_graph.py` on every run. Surfaces any user-created scripts from prior attempts in the system prompt. Routes to `VERIFY` (bug fix) or `VERIFY_ADDITIVE` (additive).

### Tool set (`tools/`)

Four tools only: `run_shell`, `read_file`, `write_file`, `replace_in_file`. `run_shell` uses a clean env (`PAGER=cat`, `TQDM_DISABLE=1`, `NO_COLOR=1`, etc.) to suppress noise. The agent writes custom helpers to `_agent_scripts/` when a bash one-liner is insufficient.

### SWE-bench eval flow (`swe_bench_run.py`)

1. Clone repo at `base_commit`; apply `test_patch` (new failing tests) as committed change
2. `pip install -e .` the target repo
3. `run_agent()` — runs the devloop engine
4. `evaluate()` — **independent** post-run check of `fail_to_pass` and `pass_to_pass`. This is the ground-truth score. Separate from `VERIFY`'s internal checks.

### Config (`cloud_agent/config.py`)

All settings from env vars:

| Var | Default | Purpose |
|-----|---------|---------|
| `OPENAI_API_KEY` | — | Required |
| `IMPLEMENT_MODEL` | `gpt-5.4-mini` | Main agent model |
| `DISCOVERY_MODEL` | `gpt-4o-mini` | Task classifier + additive context scoring |
| `TRACES_DIR` | `traces/` | JSONL event traces per session |

On Modal: env via `modal secret create openai-keys OPENAI_API_KEY=sk-...`. DB and traces redirect to `/tmp/`.

### Observability

`observability/tracer.py` — `Tracer.emit(event, payload)` writes JSONL to `traces/<session_id>.jsonl`. Key events: `node.start/complete/error`, `model_response`, `tool_call`, `gather_context.*`, `verify.result`, `verify.retry`, `verify.passed`, `history.compressed`.

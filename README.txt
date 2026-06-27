Done
* memory compression after 20 conversation turns
* prompt injection for likely relevant files
* cut down tooling from 11 to 4: run_shell, read_file, write_file, replace_in_file (rely on LLM strength in reliable unix patterns)
* agent is able to make custom python scripts to tighten devloop if it thinks it can condense 10+ bash lines (node)
* agent is encouraged to create mirror/twin minimal repo w files relevant to error, then reproduce identical error msg (node)
* set temperature=0
* environmental noise suppression: PAGER=cat, MANPAGER=cat, TQDM_DISABLE=1, PIP_PROGRESS_BAR=off -- eliminates the garbage (progress bars, pagers, ANSI codes) that eats context tokens and confuses models
* force agent to make one bash exec per turn
* model-agnostic routing: cheap models for file discovery, strong models for patch generation
* reproducibility pipeline (not optional): Analyze codebase, write a reproduction script that fails, edit source code to fix it, verify the repro now passes, test edge cases, implement on actual repo
* added a simple classifier for "bug fix" vs "additive" tasks --- if additive task fails, it goes into bug fix mode but without the reproduce-issue steps
* SWE-bench eval harness (swe_bench_run.py): eval_mode flag skips mirror-building, uses planted FAIL_TO_PASS/PASS_TO_PASS test IDs for deterministic repro confirmation and verify

Improvements
1. better testing environment: running tests / subagent sessions are all via "git worktree add -b" instead of a separate docker container that requires cold start
3. multiple attempts + retry metadata: failed command, suspected cause, changed files, why prior fix failed, next constraint. Feed that into the next attempt and block exact repeated commands/patches unless justified
4. give it self-evo abilities by letting it write Python scripts for itself mid-run that persist/compound during the run
9. linear ReAct history: dead simple, every turn appends to the same message list. No tree search, no branching, no subsession splits. The model just keeps going


- in live-swe-agent, are custom scripts / reproducing optional or pipelined?

* best ideas ive seen:
    - making dependency graphs (or simply telling the agent to pay special attention to dependencies) of the files that produce errors -- allowing easy traversal upstream/downstream to find the problem
    - giving a separate agent grep and assigning it / training it to specialize on gathering useful context (making our own might be out of scope here),
    - side-stepping all that entirely and just letting the agent make its own custom tools --- but how in the world do we make the agent so darn creative its able to make custom scripts/tool calls that reveal so much useful information to solve the problem? do we just give it like unlimited turns or something?

* walkie-talkie daemon for local multi-agent orchestration
* failure handling: sharp decomposition test ("is the seam a data/interface boundary or shared mutable state?"), bounded retries (3) before escalating, "trust but verify" (re-run the child's checks yourself, never accept its self-report), and a blocked_on taxonomy (SECRET: / DECISION: / ACCESS: / EXTERNAL:) for batched human escalation.
* if model stuck, best-of-N speculative attempts (spawn several workers on same blocked/difficult/ambiguous task)
* orphan agent reconcilliation on daemon restart
* Web search to look up documentation, history of related work, best practices, 
* shouldnt there be preliminary descriptions for each tool call so the LLM can understand what each tool call does? Kind of like loading skill.md files?
* 150-line capped reads for file discovery is a limitation



references
- opencode
- openharness https://github.com/HKUDS/OpenHarness
- openautocoder/live-swe-agent
- openautocoder/agentless
- walkie-talkie (https://github.com/xyuzh/walkie-talkie)
- claude code behavior: read/bash/edit (we could stick with this but direct it to Reproduce)

clear product vision
- harness for devin/minion-like coding agent, with autonomy to commit/push, experiment, and test environment
- TUI to multitask and manage multiple agents in one codebase simultaneously
- goal is to max out swe-bench-lite 




-----------------------------------------------------------------------------------------------------------------------





The Problems (concrete)

1. Context gathering is useless. _find_task_adjacent_files scores file paths by keyword match, reads first 150 lines. For the sympy case it would find symbol.py but miss basic.py entirely because "basic" isn't in the issue text. It never looks at what the failing tests actually import.

2. No tool-creation culture. LAYER_BASH_SKILLS mentions _agent_scripts/ for "complex/repeated operations" but gives no recipes. The agent needs permission + a specific mental model of when to create tools. Live-SWE-agent's insight: one reminder sentence per turn nearly doubled tool creation.

3. Subsessions restart cold. Each time VERIFY_FIX fails and routes back to 04_IMPLEMENT_TASK, the agent gets a fresh empty message history. It re-greps the same files from scratch. The _agent_scripts/ dir persists but the agent doesn't know what's in it.

4. SEED_SCRIPTS plants the wrong tool. docker_run.py is useless in Modal (no Docker) and wasn't used once in the trace. The seed slot should plant diagnostic tools, not infra scaffolding.

---
The Plan (prioritized by impact/effort)

Priority 1 — Prompt: tool creation recipes + per-turn reflection nudge

Files: agent/prompts.py
Effort: 1 hour

Add a LAYER_DIAGNOSTIC_TOOLS block to the system prompt with explicit if→then patterns:

If you see a failing test involving inheritance/slots/dict:
  python -c "from module import Cls; print([(c.__name__, c.__dict__.get('__slots__','MISSING')) for c in Cls.__mro__])"

If you see an ImportError or AttributeError:
  python -c "import importlib, sys; m = importlib.import_module('pkg'); print
  Then grep for the missing name: grep -rn "def missing_name\|class missing_name" .

If you need to understand what a test actually tests:
  Read the test file FIRST. Extract the class/function names it imports.
  Then grep those names in the source tree — not the tests directory.

If the same bash operation needs to run more than twice, write it as a script
in _agent_scripts/ with a descriptive name. Call it via run_shell.

Also add a single line to NUDGE_TEMPLATE (already in prompts.py, fires every
- Consider: would a diagnostic script in _agent_scripts/ reveal what you need in one call?

---
Priority 2 — SEED_SCRIPTS: plant mro_check.py instead of (or alongside) docke

Files: blueprints/nodes/seed_scripts.py
Effort: 1 hour

Replace the docker_run script with (or add next to it) _agent_scripts/mro_check.py:

#!/usr/bin/env python3
"""
Purpose: Dump the full MRO of a Python class showing __slots__ presence at each level.
Problem: __slots__ bugs require seeing the entire inheritance chain — reading
         by one misses the problematic ancestor.
Usage: python _agent_scripts/mro_check.py sympy.core.symbol.Symbol
       python _agent_scripts/mro_check.py django.db.models.Model
"""
import sys, importlib
dotted = sys.argv[1]
mod_path, cls_name = dotted.rsplit(".", 1)
mod = importlib.import_module(mod_path)
cls = getattr(mod, cls_name)
for c in cls.__mro__:
    slots = c.__dict__.get("__slots__", "*** MISSING ***")
    print(f"{c.__module__}.{c.__name__:40s}  __slots__ = {slots}")

And _agent_scripts/import_graph.py:
"""
Purpose: Show what a Python file imports and what imports it (reverse deps vi
Problem: Following import chains manually takes 10+ bash calls.
Usage: python _agent_scripts/import_graph.py sympy/core/symbol.py
"""

These give the agent specific, named tools it can reach for on turn 1 instead of discovering the need after 20 turns of confusion.

---
Priority 3 — Context: test-driven file finding for eval_mode

Files: agent/context.py
Effort: 2-3 hours

Throw out _find_task_adjacent_files for eval_mode entirely. Replace with:

1. Parse FAIL_TO_PASS test IDs → locate the test files
2. Read those test files → extract all imported names (ast.parse)
3. For each imported name, grep the source tree for its definition
4. For any class found, extract its base classes (1 level up)
5. Return: [{file, line_start, line_end, why}] — specific ranges, not first-150-lines

This is a deterministic 5-step pipeline that mirrors what SWE-grep does with RL — except we're not training anything, we're just using the failing tests as the oracle they already are.
The test knows exactly what broke; we just follow the imports.

For normal mode: swap keyword-in-path scoring for keyword-in-content grep (aln tools/search.py that wraps ripgrep — just use it).

---
Priority 4 — Cross-subsession tool memory

Files: blueprints/nodes/implement_task.py, agent/prompts.py
Effort: 1 hour

Before building the system prompt in ImplementTaskNode.run(), scan _agent_scr

existing_scripts = [
    f for f in os.listdir(Path(state.workspace_path) / "_agent_scripts")
    if f.endswith(".py") and f != "docker_run.py"
]
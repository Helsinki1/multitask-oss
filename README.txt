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
    - side-stepping all that entirely and just letting the agent make its own custom tools --- but how in the world do prompt/inspire the agent so well it's able to make custom scripts/tool calls that reveal so much useful information to solve the problem?

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



What was built and why

Workstream A — Traceback-driven context (agent/context.py)

_run_failing_tests: Runs up to 3 FAIL_TO_PASS test IDs with --tb=long before the agent starts. Limited to 3 tests to keep it fast; --tb=long gives full frame info.

_parse_traceback: Handles both traceback formats — standard Python (File "path", line N) and pytest short format (path:N: in func). Filters to files that (a) exist and (b) are inside the workspace, which excludes stdlib and site-packages automatically. Non-test source files are ordered first because they're where the bug lives; test files go to end since the agent already knows about them from fail_to_pass.

_build_import_subgraph: Pure AST parsing — no code execution. Walks import and from X import Y statements, resolves each to a .py file in the workspace via _module_to_file. One hop catches transitive deps (e.g. for sympy: test_basic.py → symbol.py, basic.py directly, then symbol.py → assumptions.py). Two hops is available but one is usually enough and keeps the file list manageable.

_find_range_around_line: When we know the errored line from the traceback, we show the surrounding function/class body, not the whole file. Walks backward to find the enclosing def/class header, forward to the next same-level definition. Caps at 80 lines so we don't dump 2000-line classes.

_find_files_from_test_imports: Fallback for when pytest isn't installed yet. Statically traces AST imports from the test files — weaker than traceback parsing but better than nothing.

build_context_bundle: Now accepts fail_to_pass. Two completely separate paths: eval mode uses the traceback pipeline, normal mode uses the LLM call. The old keyword search is gone entirely.

---
Workstream B — LLM file selector (_gather_context_with_llm)

Design: Single LLM call, not a multi-turn agent. The repo map already lists all files with their key symbols; a single call asking "which of these files are relevant?" gets 85% of the quality of a multi-turn grep agent at 1/10 the cost and latency. Multi-turn agent adds ~$0.05 and 20 seconds for marginal gain.

Returns {path, why} only — not content. build_context_bundle reads the files separately. This means the mock in tests only needs to return path+why, and file reading (the 150-line cap) is tested independently.

discovery_model — same cheap model used for task classification. Falls back silently to [] on any error; the agent can still explore on its own.

---
Workstream C — Tool-creation culture (agent/prompts.py, seed_scripts.py, implement_task.py)

Reflection sentence in LAYER_1_BASE: "After each tool result, write ONE sentence: what you now know and what you need next." This directly addresses the loop behavior in the sympy trace — the agent ran the same grep 4+ times across subsessions without articulating what was missing. Forcing one sentence creates a feedback loop.

"Stop repeating" rule: Added to LAYER_1_BASE: if you've run the same search twice without new findings, write a diagnostic script. This is the exact failure mode from the sympy trace and is stated as a hard rule rather than a suggestion.

LAYER_DIAGNOSTIC_TOOLS: Contains a concrete 8-turn narrative — "Turn 3: pytest fails. Turn 4: write mro_check.py. Turn 5: run it, output shows StdFactKB is the culprit. Turn 6: grep. Turn 7: fix. Turn 8: passes." Then explicitly names what the bad pattern looks like. Concrete examples are more influential than abstract rules.

mro_check.py and import_graph.py in seed_scripts.py: Two scripts the agent should reach for before inventing equivalent tools. Described in LAYER_2_ENV so the agent knows they exist on turn 1, not after it's already spent 10 turns failing.

_collect_agent_scripts in implement_task.py: Scans _agent_scripts/ for user-created scripts (excluding the seeded ones), extracts the Purpose: line from each docstring, and injects them via LAYER_EXISTING_TOOLS_TEMPLATE into the next subsession. When the agent writes check_ancestry.py in subsession 1 and fails, subsession 2 starts knowing that script exists — it doesn't re-derive the same diagnostic.

build_implement_human: Now includes lines="N-M" and why="..." attributes on each file tag when available, directly from the traceback-driven metadata. The agent sees exactly why each file was selected and which line range was errored.
Done
* memory compression after 20 conversation turns
* error tracebacks and dependency graphs for context gathering
* cut down tooling from 11 to 4: run_shell, read_file, write_file, replace_in_file (rely on LLM strength in reliable unix patterns)
* agent is able to make custom python scripts to tighten devloop if it thinks it can condense 10+ bash lines (node)
* set temperature=0
* environmental noise suppression: PAGER=cat, MANPAGER=cat, TQDM_DISABLE=1, PIP_PROGRESS_BAR=off -- eliminates the garbage (progress bars, pagers, ANSI codes) that eats context tokens and confuses models
* force agent to make one bash exec per turn
* model-agnostic routing: cheap models for file discovery, strong models for patch generation
* added a simple classifier for "bug fix" vs "additive" tasks --- leading to different agent node traversals
* after every IMPLEMENT subsession, the agent gathers all the context it gathered and puts it in ContextBundle dict to pass to the next IMPLEMENT session, reducing re-reads (compress still applies after 20th turns)
* after every IMPLEMENT subsession, the agent writes notes to itself that it injects into its instruction prompts in the next session
* use majority-vote grep algorithm to map swe-bench's test IDs (plain function names) to file path where test is located (for proper execution and traceback) -- this is on a per-test-ID basis
* in GATHER CONTEXT, we automatically extract the first 50 lines of the traceback file AND 150 lines near the error site, AND make an AST + let cheap LLM rank additional function handles that the agent should read next using read tool
* ensure that unrelated baseline tests that were failing from the start anyway DO NOT count as regressions and are NEVER injected into prompts (ignore them, theyre not our problem)

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
- disecting swe bench leaderboard: https://arxiv.org/pdf/2506.17208v1

clear product vision
- harness for devin/minion-like coding agent, with autonomy to commit/push, experiment, and test environment
- TUI to multitask and manage multiple agents in one codebase simultaneously
- goal is to max out swe-bench-lite 




-----------------------------------------------------------------------------------------------------------------------

LEARNINGS & INSIGHTS

the coding agent is still failing in almost the same identicaly way. i think we need a total annhilation-level gutting of the current
  architecture and start from a clean slate from first principles. The overall architecture should look like this: 1) check branch 2) load
  task IF BUG FIX: 3) run tests 4) use traceback to gather context upstream/downstream of errored files 5) implementing-fix subsession 6)
  verify fix (checking for fail-to-pass and pass-to-pass) IF FAIL-to-PASS still failing, go back to 5) implement-fix subsession, ELSE IF
  pass-to-pass now failing, go back to 3-4) use tracebacks from tests to gather context and so forth. ELSE IF (not bug fix) TASK IS
  ADDITIVE: 3) use a special context gathering algorithm/tool call to let agent make a repo map and dependency graph 4) define desired
  behavior / desired final state for the codebase 5) make a rubric and test cases for verification 6) implement subsession 7) verify
  implementation using rubric/test cases IF FAIL: go back to 4-5) question desired behavior and try implementing again ---------------- you
  see how this is much more organized than what we have now? I also think these are great ideas to champion: we NEED a clear to-do list to
  keep track of fail-to-pass cases we need to solve AS WELL AS pass-to-pass regressions we need to address; instead of prompting the agent
  to make repo maps or dependency graphs, we write an algorithm and turn it into a tool call the agent should use at specified steps for
  context gathering ------------- let me hear your thoughts on this

right: traceback-driven context gathering, diff retry loops for diff failure types (f2p p2p-regression), (for additive) algorithmic dep-graph context gathering as a tool call, structured to-do list (deterministic by test case, NOT llm's choice)
wrong: letting llm write desired behavior w/o strict verifiable contracts (test-step will write trivial tests), CAREFULLY frame completed work when moving onto future tasks, 

- each retry can get very repetitive (same file rereads, same patches/tests ran, same reasoning results...)
- 3 retry + verify attempts ARENT enough, swe-bench cases that regress need a lot more reasoning steps


-------------------------------------------------------------------------------------------------
HILLCLIMB SESSION — sympy__sympy-20590

The harness currently assumes pytest for everything. Even after resolving a Django test ID to tests/app/test_foo.py::TestClass::test_method, that only works if pytest-django is installed and DJANGO_SETTINGS_MODULE is set. The resolution is one layer — the runner assumption is another.

For a truly generalizable harness you'd need:
1. Test runner detection — check for pytest.ini, manage.py, go.mod, package.json test scripts, etc.
2. ID normalization per runner — convert bare IDs to the native format for that runner
3. Runner-aware execution — pytest, python -m django test, go test -run, npm test -- --testNamePattern= etc.

the right abstraction is a TestRunner interface with resolve_id() and run_test() methods, selected by detecting the project's test infrastructure — rather than assuming pytest universally.

-------------------------------------------------------------------------------------------------
HILLCLIMB SESSION — sympy__sympy-12236, sympy__sympy-12454, sympy__sympy-11897

ISSUES DIAGNOSED:

1. Oscillation / context thrashing: When the agent's fix for the f2p bug introduces p2p regressions,
   GATHER_CONTEXT re-enters and REPLACES the entire context bundle with regression traceback files,
   discarding the prior context. The next IMPLEMENT then reverts the original fix to clear the
   regression, and the cycle repeats until MAX_VERIFY_ATTEMPTS is exhausted.

2. Massive p2p token bloat: A structural regression (e.g. modifying a method signature) can break
   100+ tests in one file. Showing all failing tests with full tracebacks inflates LLM input to
   ~30k tokens per turn — 4x the cost and drowning the signal in noise.

3. Probe loop / empirical spinning: The agent runs 9+ consecutive run_shell/Python probes to
   understand a function's behavior without making any source file edits, burning turns that should
   be used for targeted fixes.

4. No "don't revert" guard on p2p retry: When re-entering IMPLEMENT after a p2p regression, the
   agent had no explicit signal that the f2p tests were NOW PASSING, so it would revert the working
   polys/source fix to clear the regression rather than finding a unified solution.

INNOVATIONS / FIXES MADE:

Strategy A — Anti-oscillation (3 items)

These address the same root cause (agent reverts working fix when p2p regressions fire):

- #1 Context preservation in p2p re-entry: Load-bearing. Without it, GATHER_CONTEXT wipes the context bundle and the agent loses sight of what it was fixing. This is the direct fix for the oscillation mechanism.
- #8 Baseline p2p tracking + routing: Load-bearing. Changed 12454 from 23 false p2p regressions (triggering oscillation) to 0 newly_failing. This is what actually stopped the loop.
- #2 "CRITICAL: do not revert" in p2p retry prompt (prompts.py): _BUGFIX_RETRY_P2P opens with an
   explicit warning that f2p tests are NOW PASSING — agent must keep current changes and find a
   unified fix, not revert.

Strategy B — Token budget / signal density (3 items)

- #3 Capped p2p list: Load-bearing. 100+ tests × full tracebacks = 30k tokens. Max 5 shown is clearly the right call.

Strategy C — Harness guardrails (3 items)
                                                                                                                                           
- #7 Git revert + prefer replace_in_file: Load-bearing.git checkout HEAD -- polyclasses.py at turn 29 and immediately found the right fix. Agents were consistently using fragile Python heredoc patches instead of replace_in_file — the guidance changed that behavior.
- #9 Budget-exhausted no-op: Load-bearing in its narrowness. Directly observed in bp37b3wp8: IMPLEMENT with 0 turns → CHECKPOINT instead of 3 empty VERIFY loops. Small but clean.
- #5 Programmatic probe-loop nudge (subsession.py): After 4 consecutive run_shell calls without any
   file edit, the harness injects a forced-edit message. Addresses agents that empirically spin
   through 10+ probes without making progress.

-------------------------------------------------------------------------------------
Hillclimb Session 3

Problem 1: baseline_still_failing burns turns on pre-existing env issues

What you see in the trace:
verify: f2p_failing=[]  p2p_failing=[]
[VERIFY] warning → IMPLEMENT

This looks like VERIFY passes but still routes to IMPLEMENT. The trace doesn't display it, but verify.py:54 has a 3-part success condition:
if not f2p_failing and not newly_failing and not baseline_still_failing:
    → CHECKPOINT

The agent notes.md files say repeatedly: "Root cause: SymPy 3.11 compatibility issues blocked import during test collection (collections.Callable)". This means some p2p tests fail at GATHER_CONTEXT time due to a pre-existing collections.MutableMapping import error — not the actual bug. Those get recorded as baseline_still_failing. VERIFY then forces the agent to fix them too, even though the real bug is already fixed. In 12419 and 12481, the agent spends the final IMPLEMENT sessions working on Python 3.11 compat shims instead of the actual regression.

12481 ends like this: agent fixes f2p (f2p: true in external eval), but then spends turns patching sathandlers.py for MutableMapping — and that unrelated change likely introduces the p2p regression (p2p: false externally).

---
Problem 2: VERIFY runs tests individually; external eval runs them as a batch

verify.py:_run_all_tests runs each test_id separately via its own pytest process. swe_bench_run.py:evaluate passes all p2p IDs to one pytest invocation (_pytest(pass_to_pass, stop_on_first=False)).

When tests share a process, module-level state is shared across imports. A change to a module that patches a class or adds a side-effectful import can cause cross-test contamination that never appears in individual runs. This is the most likely explanation for both 12481 and 12419 passing VERIFY but failing external p2p eval.

---
Problem 3: True oscillation — test_div (12236, 60 turns, fail)

This is the most wasteful case. The agent attempts 40+ replace_in_file calls on polytools.py, running test_div after each one, always getting exit code 1. A sample of the loop:

turn 13: replace_in_file → polytools.py
turn 14: run_shell → pytest test_div → exit 1
turn 18: replace_in_file → polytools.py
turn 19: run_shell → pytest test_div → exit 1
...
turn 44: replace_in_file → Error: old_text not found  ← stale diff
[TASK] task.budget_exhausted

Each new IMPLEMENT session re-reads the same files (turn 0: read_file at line 200, turn 1: read polytools.py...) because sessions start cold. The anti-oscillation strategies from the recent commit aren't preventing this because each session is genuinely "trying something new" — just something that doesn't work. The problem requires understanding polynomial domain coercion semantics that the agent doesn't converge on.

---
Problem 4: The is_upper fix gets overwritten (12454, 31 turns, fail)

The pattern for 12454:
IMPLEMENT → fix is_upper in matrices.py → VERIFY
verify: f2p_failing=[test_is_upper, test_hessenberg] → IMPLEMENT
IMPLEMENT → same grep + same replace → VERIFY
verify: f2p_failing=[test_is_upper, test_hessenberg] → IMPLEMENT
...
[VERIFY] warning → CHECKPOINT (max retries exhausted)

Every IMPLEMENT session does the identical sequence: grep is_upper → read matrices.py → replace. But the fix keeps failing VERIFY. This is oscillation on a specific test, not drift across tests — the agent isn't making the correct fix, just repeatedly applying the same wrong one, because there's no mechanism to say "this exact replacement was already tried and failed."

---
Problem 5: Context bundle isn't addressing the root failure mode

The new feature (file header + error site lines) helps when the agent needs more context about what code is at the error site. But in these failing cases:

- 12236 (test_div): agent reads the right files but can't reason through polynomial domain coercion. More context lines don't help reasoning quality.
- 12454 (is_upper): agent finds the right function immediately, just applies the wrong fix repeatedly.
- 12481 / 12419: agent's problem is p2p side effects from unrelated edits, not insufficient context.

The 3 passes (13031, 11400, 12171) all had relatively straightforward, localized fixes. The failures are failing for structural reasons (oscillation, env noise, inter-test side effects) that more context at the start doesn't address.



BIGGEST OBSTACLE RIGHT NOW: making this pipeline extremely effective --- GATHER CONTEXT -> read headers/call-sites and note function handles -> very good logic for agent to use read-file tool to fill in the blanks it needs
- the agent simply doesnt know what is relevant + unread at the moment AND it doesnt know where to find it

Task 1 — Navigation map per file (_render_outline_map): Every context file now starts with a NAVIGATION MAP header listing all symbols (functions, classes, methods) with exact line ranges. Traceback frames are marked ⬅ TRACEBACK FRAME (shown below) and callees are marked ← callee of traceback frame — read_file to see implementation.

Task 2 — Callee annotation (_find_local_callees): For each traceback frame function, a single AST walk collects all call sites (Name.id + Attribute.attr), cross-references against the file's own outline, and surfaces local callees by name in the map. The agent now knows exactly which locally-defined functions to read_file next.

Task 3 — ranked_sections removed entirely: Deleted _rank_sections from gather_context.py, all call sites, the ranked_sections field from ContextBundle in state.py, the rendering block in prompts.py, and the construction in implement.py. No LLM call in GATHER_CONTEXT anymore — the map is built deterministically.

Also updated _TOOLS in the system prompt to reference the NAVIGATION MAP instead of the stale "Ranked sections" language, and replaced the two dead tests (_dedupe_frames, _build_import_subgraph) with tests for the three new functions.
-----------------------------------------------------------------------------------------------------------------------

HILLCLIMB SESSION — sympy__sympy-12419 (observed 2026-06-30)

ISSUES DIAGNOSED (from full modal trace observation):

Problem A: Import-phase errors erase real traceback context.
When test_matexpr.py::test_Identity fails with `ImportError: cannot import name 'Mapping' from 'collections'` in basic.py, GATHER_CONTEXT only loads basic.py (the error site). matexpr.py — where Identity._entry returns the wrong value — is never in the initial context bundle. The agent starts blind to the actual bug.

Problem B: Agent's grep patterns don't find _entry due to kwargs mismatch.
The agent greps `def _entry(self, i, j)` but the real signature is `def _entry(self, i, j, **kwargs)`. The grep returns empty and the agent concludes the method doesn't exist, then goes hunting in summations.py instead.

Problem C: Agent oscillates to summations.py after misreading the test.
Because the test has `Sum(Identity[i,j], (i,1,n), (j,1,n))`, the agent incorrectly attributes the failure to the Sum evaluation layer rather than Identity._entry. This only happens because matexpr.py isn't in initial context — if it were, the agent would immediately see Identity._entry returning S.Zero.

Problem D: Agent reverts working compat fixes mid-session.
At turn 20 history compression fires. By turn 22 the agent loses confidence in its basic.py/plot.py collections fixes (which are actually correct prerequisites) and reverts them with `git checkout HEAD`. This wipes necessary groundwork and forces re-work in the next retry.

FIXES IMPLEMENTED:

Fix 1 (agent/context.py — _frames_from_test_imports): When any f2p test fails with an import-phase error (ImportError, ModuleNotFoundError, SyntaxError, ERROR collecting), parse the test file's static imports, follow one hop through __init__.py re-exports, and add the real source files to the context bundle anchored at the line of each imported name. For 12419, this adds matexpr.py anchored at the Identity class — so Identity._entry appears in the pre-loaded navigation map from turn 0. General fix: helps any test blocked by env compat errors.

Fix 2 (agent/prompts.py — _BUGFIX_RETRY_F2P): Added anti-revert note: "Before reverting any of the above changes, confirm they are wrong — run the failing test with and without each change. Compatibility fixes may be necessary prerequisites even if the main test still fails afterward." Prevents the agent from undoing correct compat patches when it gets stuck on the remaining bug.

Problem E: Pre-fix of `from collections import X` missed attribute-access pattern.
Fixing only `from collections import Mapping` (3 files) left 4 p2p tests failing at baseline due to `collections.Callable` attribute access (e.g. `isinstance(x, collections.Callable)`) in other modules, causing pass_to_pass=false in external eval.

Problem F: `_frames_from_test_imports` did not fire for assertion failures.
After the collections pre-fix, `test_matexpr.py` could be collected and the test ran but failed on assertion (exit code 1, not 4). The `_is_import_phase_failure` guard prevented `_frames_from_test_imports` from adding matexpr.py to the initial context — so the agent started with only 2.4k tokens of context and could not see the bug.

Problem G: Agent used inline Python `text.replace(old, new)` which silently fails.
When `old` text doesn't exactly match (whitespace, indentation), `text.replace` returns the unchanged file with no error. The agent wasted 20+ turns on silent no-op patches, never seeing that its "change" had no effect. The test kept failing and the agent exhausted the turn budget.

Fix 3 (swe_bench_run.py — `_fix_collections_compat`): Extended collections compat pre-fix to also handle `collections.Callable` attribute-access patterns using a regex sweep, not just `from collections import X` (AST). Now catches all 13 affected files, reducing baseline p2p failures from 25 to 0 before the agent starts. Committed as a separate git commit so the agent's diff only shows the actual bug fix.

Fix 4 (agent/context.py — `_frames_from_test_imports`): Removed the `_is_import_phase_failure` guard. The function now fires for ALL failing f2p tests, not just import-phase errors. When the test runs but only has test-file frames in the traceback (assertion), this adds the implementation files (matexpr.py with Identity class anchored) to the initial context. General fix: any test where the tested code is not in the traceback.

Fix 5 (agent/prompts.py — `_TOOLS`): Changed the inline-Python guidance from "prefer replace_in_file" to "ALWAYS use replace_in_file — never Python inline patches", with an explicit explanation of WHY: text.replace silently fails with no error when old text doesn't match. This eliminates the wasted retry loop from silent no-op patches.


The official SWE-bench harness uses conda to spin up a per-instance Python environment (3.7, 3.8, 3.9, etc., depending on the repo's era). Our Modal setup uses a single fixed Python version for everything. The right solution is just to match what SWE-bench does — bake multiple Python versions into the Modal image and pick the right one per instance based on the instance metadata. Modal image builds are cached, so once built the cold start difference would be negligible.

The _fix_collections_compat pre-patch is a quick workaround for a symptom (collections import errors) of the real mismatch (wrong Python version). It's also slightly risky: it modifies the workspace before the agent starts in a way SWE-bench doesn't define, which could theoretically change semantics or produce false positives on other instances.
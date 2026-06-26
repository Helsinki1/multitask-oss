Done
* memory compression after 20 conversation turns
* prompt injection for likely relevant files
* cut down tooling from 11 to 4: run_shell, read_file, write_file, replace_in_file (rely on LLM strength in reliable unix patterns)
* agent is able to make custom python scripts to tighten devloop if it thinks it can condense 10+ bash lines (node)
* agent is encouraged to create mirror/twin minimal repo w files relevant to error, then reproduce identical error msg (node)
* set temperature=0
* environmental noise suppression: PAGER=cat, MANPAGER=cat, TQDM_DISABLE=1, PIP_PROGRESS_BAR=off -- eliminates the garbage (progress bars, pagers, ANSI codes) that eats context tokens and confuses models
* force agent to make one bash exec per turn

Improvements
1. better testing environment: running tests / subagent sessions are all via "git worktree add -b" instead of a separate docker container that requires cold start
3. multiple attempts + retry metadata: failed command, suspected cause, changed files, why prior fix failed, next constraint. Feed that into the next attempt and block exact repeated commands/patches unless justified

4. give it self-evo abilities by letting it write Python scripts for itself mid-run that persist/compound during the run
5. reproducibility pipeline (not optional): Analyze codebase, write a reproduction script that fails, edit source code to fix it, verify the repro now passes, test edge cases, implement on actual repo



9. linear ReAct history: dead simple, every turn appends to the same message list. No tree search, no branching, no subsession splits. The model just keeps going
10. model-agnostic routing: cheap models for file discovery, strong models for patch generation

- in live-swe-agent, are custom scripts / reproducing optional or pipelined?

(5, 1) 4, 6, 9, (8, 7), 3

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



Thoughts

- Tiered landings for tool discovery
    - web search
    - spin up local servers (ex: npm, flask)
        - install dependencies
        - set up testing environments
    - use the browser, capture screenshots, reason via VLMs

- Tiered landings for repo navigation

- multi-task by spawning subagents
    - planning
    - work checker
    - work queue + worker agents

- 3D UI to help track each agent (mind palace)
    - https://github.com/asheshgoplani/agent-deck
    this but better
    Python (Textual), Go (Bubble Tea / Lipgloss), Floors = Tabs/Views; Rooms = Named Grid Containers
    tmux backend, textual-terminal widgets
- google account integrations
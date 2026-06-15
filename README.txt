Done
* memory compression after 20 conversation turns
* prompt injection for likely relevant files
* terminal commands, background processes

Improvements
1. better testing environment: every test that involves a non-trivial app should open Docker and run servers and requests scripts inside it
2. better tools: changed-file gathering, language-aware lint/format/test selection, and a related-tests loop
    * after 04_IMPLEMENT_TASK, every run records changed files, selected test commands, exit codes, and final diff/test evidence
    * expand tool metadata toward the plan, validate schemas, include examples/limits, and make tool results structured enough to summarize failures
3. multiple attempts + retry metadata: failed command, suspected cause, changed files, why prior fix failed, next constraint. Feed that into the next attempt and block exact repeated commands/patches unless justified

* Web search to look up documentation, history of related work, best practices, 
* shouldnt there be preliminary descriptions for each tool call so the LLM can understand what each tool call does? Kind of like loading skill.md files?
* 150-line capped reads for file discovery is a limitation









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
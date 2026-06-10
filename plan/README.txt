================================================================================
CLOUD CODING AGENT — MASTER BUILD PLAN
================================================================================

Version: 2026-06-09
Status: Living document. Synthesized from README.txt, roadmap.txt, openclaw-arch.txt.

This plan describes the full architecture, implementation phases, and build
checklist for a cloud-native autonomous coding agent that can:

    1. Accept tasks from any channel (CLI, web, Slack, Telegram, WhatsApp, API)
    2. Execute coding tasks in isolated sandboxes with a deterministic blueprint
    3. Spawn and coordinate subagents for planning, review, and parallel work
    4. Let users define prompt loops — recurring or conditional agent behaviors
       the user programs as first-class objects, not one-off prompts

This system is inspired by three sources:
    - The Stripe Minion architectural pattern (roadmap.txt):
        Deterministic envelope around an autonomous LLM implementation core.
    - OpenClaw's runtime pattern (openclaw-arch.txt):
        Channel adapters, session manager, queue, WebSocket control plane,
        and Markdown-based persistent context files.
    - README.txt ideas:
        Tiered tool discovery, browser + VLM tools, local server tools,
        3D mind-palace agent UI, subagent work queues.

================================================================================
HOW TO USE THIS PLAN
================================================================================

Read sections in order for a complete picture, or jump to:

    01-vision/          — What and why
    02-architecture/    — Full system map and component descriptions
    03-devloop-blueprint/ — The 20-node coding pipeline, node by node
    04-subagent-system/ — Spawning subagents, prompt loops, work queues
    05-tool-system/     — Tool registry, coding tools, discovery, browser, servers
    06-sandbox/         — Isolation, Docker, warm pools, credentials
    07-ui-and-control-plane/ — Channel adapters, session manager, 3D UI
    08-context-and-prompts/ — Context pipeline, prompt layering, memory files
    09-model-gateway/   — Model routing, spend, fallback
    10-state-and-persistence/ — DB schema, artifacts, resume/cancel
    11-observability/   — Tracing, metrics, evals
    12-safety-and-security/ — Threat model, approval gates, public repo safety
    13-roadmap/         — Phased build plan, week-by-week, acceptance tests

================================================================================
NORTH STAR
================================================================================

A user should be able to:

    1. Describe a task in plain English via any channel.
    2. Watch the agent work, see logs, diffs, and test results.
    3. Define a loop: "Every time a new issue is filed with label 'bug', run
       my standard triage prompt." or "After each PR is merged, run my
       changelog-generation prompt."
    4. Spawn subagents for complex decompositions: planner breaks the work,
       workers implement each chunk, reviewer checks the assembled diff.
    5. Trust the result: every action is logged, every test run is recorded,
       every push was gated by deterministic safety checks.

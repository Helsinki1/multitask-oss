"""Node GATHER_CONTEXT: agentic read-queue subsession for bug-fix context gathering.

Seeds a read-queue from the failing test's traceback (or, on a targeted VERIFY
re-entry, from VERIFY's already-run TestToDoList — no redundant test execution,
see seed_gather_context in agent/context.py), then runs a restricted-tool LLM
subsession that must drain the queue and note() every file it reads before
finishing (enforced by on_finish_check + the dequeue/note discipline in
tools/gather_context_tools.py). Notes become the ContextBundle IMPLEMENT starts
from.

Handles both the true first entry and VERIFY's capped, targeted recontext
re-entry (p2p regression / f2p new error signature) — see MAX_RECONTEXT_ATTEMPTS
and recontext_reason in blueprints/nodes/verify.py.
"""

from __future__ import annotations

import os

from agent.context import (
    _detect_build_commands,
    _load_repo_rules,
    _read_file_lines,
    seed_gather_context,
)
from agent.prompts import build_gather_context_human, build_gather_context_system
from agent.runtime import Node, NodeResult
from agent.state import AgentState, ContextBundle
from agent.subsession import SubsessionConfig, run_subsession
from cloud_agent.config import settings
from observability.tracer import Tracer
from tools.gather_context_tools import GatherQueueState, build_gather_context_toolset
from tools.registry import ToolRegistry, ToolSpec
from tools.shell import run_shell_tool

GATHER_CONTEXT_MAX_TURNS = 20
_MAX_PRIOR_FILES_CARRIED = 5


class GatherContextNode(Node):
    name = "GATHER_CONTEXT"
    node_type = "llm_subsession"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path
        is_recontext = state.verify_failure_type in ("p2p_regression", "f2p_new_error")
        prior_todo_list = state.todo_list if is_recontext else None

        todo_list, seed_files = seed_gather_context(
            ws, state.fail_to_pass, state.pass_to_pass, prior_todo_list,
        )

        self.tracer.emit("gather_context.start", {
            "mode": "recontext" if is_recontext else "initial",
            "verify_failure_type": state.verify_failure_type,
            "recontext_reason": state.recontext_reason,
            "seed_files": seed_files,
            "f2p_failing": [c.test_id for c in todo_list.f2p_failing],
        })

        qs = GatherQueueState()
        qs.seed(seed_files)

        registry = ToolRegistry()
        build_gather_context_toolset(registry, qs)
        registry.register(_run_shell_spec())

        def on_finish_check() -> str | None:
            if qs.queue:
                return (
                    f"[harness] Your read-queue still has {len(qs.queue)} file(s) "
                    f"pending: {qs.queue}. Call dequeue_next() to continue — you "
                    "cannot finish with a non-empty queue."
                )
            return None

        config = SubsessionConfig(
            name="gather_context",
            system_prompt=build_gather_context_system(state, state.recontext_reason),
            initial_human_message=build_gather_context_human(state, seed_files),
            model=settings.discovery_model,
            tools_schema=registry.to_openai_schema(),
            max_turns=GATHER_CONTEXT_MAX_TURNS,
            max_wall_seconds=state.budgets.max_wall_seconds,
            max_tokens=8192,
            on_finish_check=on_finish_check,
        )

        result, updated_state = run_subsession(config, state, registry, self.tracer)

        self.tracer.emit("gather_context.subsession_done", {
            "status": result.status,
            "turns": result.total_turns,
            "cost_usd": result.total_cost_usd,
            "files_noted": sorted({n["path"] for n in qs.notes}),
        })

        # If budget was already exhausted before this subsession did any work, skip
        # straight to CHECKPOINT — mirrors ImplementNode's identical guard, since
        # continuing to IMPLEMENT would just immediately budget-exhaust again too.
        if result.status == "budget_exhausted" and result.total_turns == 0:
            self.tracer.emit("gather_context.budget_exhausted_noop", {})
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={"budgets": updated_state.budgets},
                status="warning",
            )

        new_files = _render_notes(qs.notes)

        # Preserve prior-attempt files not covered by this pass's notes, refreshed from
        # disk — load-bearing carryover (see README "Context preservation in p2p
        # re-entry"): without it, a recontext pass that only notes the regression
        # tracebacks can make IMPLEMENT lose visibility of files it was already editing.
        prior_carried: list[dict] = []
        if is_recontext:
            new_paths = {f["path"] for f in new_files}
            for pf in state.context_bundle.task_adjacent_files:
                if pf["path"] in new_paths:
                    continue
                abs_path = os.path.join(ws, pf["path"])
                if not os.path.isfile(abs_path):
                    continue
                content = _read_file_lines(abs_path, max_lines=120)
                if content:
                    prior_carried.append({
                        "path": pf["path"],
                        "content": content,
                        "why": "from prior attempt (current on-disk state — do not revert)",
                    })
                if len(prior_carried) >= _MAX_PRIOR_FILES_CARRIED:
                    break

        cb = ContextBundle(
            repo_rules=_load_repo_rules(ws),
            build_and_test_commands=_detect_build_commands(ws),
            task_adjacent_files=new_files + prior_carried,
        )

        self.tracer.emit("gather_context.done", {
            "context_files": [f["path"] for f in cb.task_adjacent_files],
            "prior_files_carried": [f["path"] for f in prior_carried],
            "mode": "recontext" if is_recontext else "initial",
        })

        state_update: dict = {
            "todo_list": todo_list,
            "context_bundle": cb,
            "budgets": updated_state.budgets,
        }

        # Baseline p2p failures (pre-existing env issues, not agent-caused regressions)
        # only need capturing on the true first entry — VERIFY already carries this
        # forward on every subsequent turn via its own state_update.
        if not is_recontext:
            baseline_failing = [
                c.test_id for c in todo_list.cases
                if c.category == "pass_to_pass" and c.status == "failing"
            ]
            state_update["baseline_p2p_failing"] = baseline_failing
            if baseline_failing:
                self.tracer.emit("gather_context.baseline_p2p_failing", {
                    "count": len(baseline_failing),
                    "test_ids": baseline_failing[:10],
                })

        return NodeResult(
            next_node="IMPLEMENT",
            state_update=state_update,
            status="ok" if result.status == "done" else "warning",
        )


def _run_shell_spec() -> ToolSpec:
    return ToolSpec(
        name="run_shell",
        description=(
            "Run any bash command in the repository. Use grep/find to locate callers, "
            "definitions, or similar patterns, then enqueue() what looks relevant. "
            "Read-only exploration only — no editing tools are exposed in this session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Working directory relative to repo root"},
                "timeout_seconds": {"type": "integer", "description": "Timeout (default: 120s, max: 600s)"},
            },
            "required": ["command"],
        },
        fn=run_shell_tool,
        timeout_seconds=120,
    )


def _render_notes(notes: list[dict]) -> list[dict]:
    """Group GatherQueueState.notes by path into ContextBundle.task_adjacent_files entries.

    Notes marked "not relevant" are dropped. Multiple notes on the same path are
    ordered by line number and joined into one block.
    """
    by_path: dict[str, list[dict]] = {}
    for n in notes:
        by_path.setdefault(n["path"], []).append(n)

    result: list[dict] = []
    for path, path_notes in by_path.items():
        relevant = [n for n in path_notes if n["why"].strip().lower() != "not relevant"]
        if not relevant:
            continue
        relevant.sort(key=lambda n: n["start_line"])
        blocks = [
            f'lines {n["start_line"]}-{n["end_line"]} ({n["why"]}):\n{n["code"]}'
            for n in relevant
        ]
        result.append({
            "path": path,
            "content": "\n\n".join(blocks),
            "why": relevant[0]["why"] if len(relevant) == 1 else f"{len(relevant)} relevant section(s) noted",
        })
    return result

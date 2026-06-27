"""Node GATHER_CONTEXT: deterministic traceback-driven context gathering.

Runs the failing tests, parses tracebacks, follows the import graph one hop,
and loads the causally-adjacent source files as pre-loaded context for IMPLEMENT.

Also handles the p2p-regression re-entry path: when VERIFY detects pass_to_pass
failures it routes here to re-derive context from the regression tracebacks
already stored in the TestToDoList.
"""

from __future__ import annotations

from agent.context import build_bugfix_context, rebuild_context_from_regressions
from agent.runtime import Node, NodeResult
from agent.state import AgentState
from observability.tracer import Tracer


class GatherContextNode(Node):
    name = "GATHER_CONTEXT"
    node_type = "deterministic"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path

        if state.verify_failure_type == "p2p_regression":
            # Re-entry from VERIFY after regression: re-derive context from p2p tracebacks
            self.tracer.emit("gather_context.p2p_retry", {
                "p2p_failing": [c.test_id for c in state.todo_list.p2p_failing],
            })
            cb = rebuild_context_from_regressions(ws, state.todo_list)
            self.tracer.emit("gather_context.done", {
                "context_files": [f["path"] for f in cb.task_adjacent_files],
                "mode": "regression",
            })
            return NodeResult(
                next_node="IMPLEMENT",
                state_update={"context_bundle": cb},
                status="ok",
            )

        # First entry (or f2p retry re-seeding context):
        # Run all tests, collect tracebacks, build context bundle
        fail_to_pass = state.fail_to_pass
        pass_to_pass = state.pass_to_pass

        self.tracer.emit("gather_context.start", {
            "fail_to_pass": fail_to_pass,
            "pass_to_pass_count": len(pass_to_pass),
        })

        todo_list, cb = build_bugfix_context(ws, fail_to_pass, pass_to_pass)

        self.tracer.emit("gather_context.done", {
            "f2p_failing": [c.test_id for c in todo_list.f2p_failing],
            "p2p_failing": [c.test_id for c in todo_list.p2p_failing],
            "context_files": [f["path"] for f in cb.task_adjacent_files],
            "mode": "initial",
        })

        return NodeResult(
            next_node="IMPLEMENT",
            state_update={
                "todo_list": todo_list,
                "context_bundle": cb,
            },
            status="ok",
        )

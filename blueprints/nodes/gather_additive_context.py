"""Node GATHER_ADDITIVE_CONTEXT: algorithmic dependency-graph context for additive tasks.

No LLM. Builds an import graph across all workspace Python files, scores by
in-degree (how many others import this module) and keyword match with the task,
and returns the top files as pre-loaded context for DEFINE_CONTRACT and IMPLEMENT.
"""

from __future__ import annotations

from agent.context import build_additive_context
from agent.runtime import Node, NodeResult
from agent.state import AgentState
from observability.tracer import Tracer


class GatherAdditiveContextNode(Node):
    name = "GATHER_ADDITIVE_CONTEXT"
    node_type = "deterministic"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        self.tracer.emit("gather_additive_context.start", {})

        cb = build_additive_context(state.workspace_path, state.task_text)

        self.tracer.emit("gather_additive_context.done", {
            "context_files": [f["path"] for f in cb.task_adjacent_files],
        })

        return NodeResult(
            next_node="DEFINE_CONTRACT",
            state_update={"context_bundle": cb},
            status="ok",
        )

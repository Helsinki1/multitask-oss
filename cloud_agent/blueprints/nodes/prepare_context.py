"""Node 03: PREPARE_CONTEXT.

Builds the ContextBundle: repo rules, build commands, repo map.
"""

from cloud_agent.agent.context import build_context_bundle
from cloud_agent.agent.runtime import Node, NodeResult
from cloud_agent.agent.state import AgentState


class PrepareContextNode(Node):
    name = "03_PREPARE_CONTEXT"
    node_type = "deterministic"
    failure_next = "04_IMPLEMENT_TASK"  # degraded context is non-fatal

    def run(self, state: AgentState) -> NodeResult:
        cb = build_context_bundle(state.workspace_path, state.task_text)
        return NodeResult(
            next_node="04_IMPLEMENT_TASK",
            state_update={"context_bundle": cb},
        )

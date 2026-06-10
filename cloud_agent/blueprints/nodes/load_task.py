"""Node 02: LOAD_TASK.

Normalizes task text and detects repo visibility.
"""

import os

from cloud_agent.agent.runtime import Node, NodeResult
from cloud_agent.agent.state import AgentState


class LoadTaskNode(Node):
    name = "02_LOAD_TASK"
    node_type = "deterministic"
    failure_next = "END"

    def run(self, state: AgentState) -> NodeResult:
        if not state.task_text.strip():
            raise ValueError("Task text is empty")

        # Derive repo_name from workspace_path
        repo_name = os.path.basename(state.workspace_path.rstrip("/"))

        # For now: treat all repos as private (is_public_repo=False)
        # Phase 2 will detect this via GitHub API

        return NodeResult(
            next_node="03_PREPARE_CONTEXT",
            state_update={
                "repo_name": repo_name,
                "is_public_repo": False,
            },
        )

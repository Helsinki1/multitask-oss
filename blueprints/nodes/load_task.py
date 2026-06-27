"""Node LOAD_TASK: normalize task, classify type, route to correct context-gathering node.

In eval_mode (SWE-bench): always bug_fix, routes to GATHER_CONTEXT.
In normal mode: one cheap LLM call classifies bug_fix vs additive.
"""

from __future__ import annotations

import os

from agent.runtime import Node, NodeResult
from agent.state import AgentState
from cloud_agent.config import settings


def _classify_task(task_text: str) -> str:
    """Cheap LLM call: classify task as 'bug_fix' or 'additive'."""
    try:
        import openai
        client = openai.OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.discovery_model,
            temperature=0,
            max_tokens=10,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify the coding task as exactly one of: bug_fix or additive.\n"
                        "bug_fix: fixing a bug, error, or failing test.\n"
                        "additive: adding a new feature, function, class, or file.\n"
                        "Reply with only the label."
                    ),
                },
                {"role": "user", "content": f"Task: {task_text[:500]}"},
            ],
        )
        label = response.choices[0].message.content.strip().lower()
        if "additive" in label:
            return "additive"
        return "bug_fix"
    except Exception:
        return "bug_fix"


class LoadTaskNode(Node):
    name = "LOAD_TASK"
    node_type = "deterministic"
    failure_next = "END"

    def run(self, state: AgentState) -> NodeResult:
        if not state.task_text.strip():
            raise ValueError("Task text is empty")

        repo_name = os.path.basename(state.workspace_path.rstrip("/"))

        if state.eval_mode:
            task_type = "bug_fix"
        else:
            task_type = _classify_task(state.task_text)

        next_node = "GATHER_CONTEXT" if task_type == "bug_fix" else "GATHER_ADDITIVE_CONTEXT"

        return NodeResult(
            next_node=next_node,
            state_update={
                "repo_name": repo_name,
                "task_type": task_type,
            },
        )

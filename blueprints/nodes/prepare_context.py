"""Node 03: PREPARE_CONTEXT.

Builds the ContextBundle and classifies the task type so downstream nodes
can take the right path (bug_fix → REPRODUCE_ISSUE, additive → IMPLEMENT_TASK).
"""

import openai

from agent.context import build_context_bundle
from agent.runtime import Node, NodeResult
from agent.state import AgentState
from cloud_agent.config import settings

_CLASSIFY_SYSTEM = (
    "Classify the software task as exactly one of:\n"
    "  bug_fix  — fix a bug, error, crash, failing test, or broken behavior\n"
    "  additive — implement new feature, write new code, add a file, solve a problem, refactor, or document\n"
    "Reply with exactly one word: bug_fix or additive."
)


def _classify_task(task_text: str) -> str:
    """Single cheap LLM call → 'bug_fix' or 'additive'. Falls back to 'bug_fix' on error."""
    try:
        client = openai.OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.discovery_model,
            temperature=0,
            max_tokens=5,
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": f"Task: {task_text[:600]}"},
            ],
        )
        answer = response.choices[0].message.content.strip().lower()
        return "bug_fix" if "bug" in answer else "additive"
    except Exception:
        return "bug_fix"  # safe default: run the full pipeline


class PrepareContextNode(Node):
    name = "03_PREPARE_CONTEXT"
    node_type = "deterministic"
    failure_next = "04_IMPLEMENT_TASK"  # degraded context is non-fatal

    def run(self, state: AgentState) -> NodeResult:
        cb = build_context_bundle(state.workspace_path, state.task_text)
        if state.eval_mode:
            # External benchmarks always provide failing tests; skip classifier.
            return NodeResult(
                next_node="REPRODUCE_ISSUE",
                state_update={"context_bundle": cb, "task_type": "bug_fix"},
            )
        task_type = _classify_task(state.task_text)
        next_node = "REPRODUCE_ISSUE" if task_type == "bug_fix" else "04_IMPLEMENT_TASK"
        return NodeResult(
            next_node=next_node,
            state_update={"context_bundle": cb, "task_type": task_type},
        )

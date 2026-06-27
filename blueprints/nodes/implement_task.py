"""Node 04: IMPLEMENT_TASK (LLM Subsession).

The main implementation loop. Model inspects, edits, runs commands.
"""

from pathlib import Path

from agent.escalation import EscalationConfig
from agent.prompts import build_implement_human, build_implement_system
from agent.runtime import Node, NodeResult
from agent.state import AgentState
from agent.subsession import SubsessionConfig, run_subsession
from cloud_agent.config import settings
from observability.tracer import Tracer
from tools.registry import ToolRegistry, build_dev_toolset

# Scripts seeded by SEED_SCRIPTS — already described in LAYER_2_ENV, excluded from
# the "previously created" list so they don't appear twice.
_SEEDED_SCRIPTS = {"mro_check.py", "import_graph.py", "docker_run.py"}


def _collect_agent_scripts(workspace_path: str) -> list[str]:
    """Return name+Purpose for user-created scripts in _agent_scripts/ (not seeded ones).

    Scripts the agent wrote in a prior subsession are surfaced here so it doesn't
    re-derive the same diagnostic from scratch on the next verification attempt.
    """
    scripts_dir = Path(workspace_path) / "_agent_scripts"
    if not scripts_dir.exists():
        return []
    result: list[str] = []
    for p in sorted(scripts_dir.glob("*.py")):
        if p.name in _SEEDED_SCRIPTS:
            continue
        summary = ""
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines()[:20]:
                stripped = line.strip().strip('"""').strip("'''")
                if stripped.startswith("Purpose:"):
                    summary = stripped[len("Purpose:"):].strip()
                    break
        except OSError:
            pass
        result.append(p.name + (f" — {summary}" if summary else ""))
    return result


class ImplementTaskNode(Node):
    name = "04_IMPLEMENT_TASK"
    node_type = "llm_subsession"
    failure_next = "05_CHECKPOINT"  # partial work still gets checkpointed

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        registry = ToolRegistry()
        build_dev_toolset(registry)

        escalation_config = EscalationConfig(
            default_model=settings.implement_model,
            escalated_model=settings.escalated_model,
            escalation_enabled=settings.escalation_enabled,
            max_escalations=settings.max_escalations,
            escalation_after_failed_completion_checks=settings.escalation_after_failed_completion_checks,
            escalation_after_repeated_tool_failures=settings.escalation_after_repeated_tool_failures,
            escalation_after_turn_fraction=settings.escalation_after_turn_fraction,
        )

        existing_tools = _collect_agent_scripts(state.workspace_path)

        config = SubsessionConfig(
            name="implement_task",
            system_prompt=build_implement_system(state, state.context_bundle, existing_tools=existing_tools),
            initial_human_message=build_implement_human(state),
            model=settings.implement_model,
            tools_schema=registry.to_openai_schema(),
            max_turns=state.budgets.max_llm_turns,
            max_wall_seconds=state.budgets.max_wall_seconds,
            max_tokens=8192,
            escalation_config=escalation_config,
        )

        result, updated_state = run_subsession(config, state, registry, self.tracer)

        self.tracer.emit("subsession.done", {
            "status": result.status,
            "turns": result.total_turns,
            "cost_usd": result.total_cost_usd,
        })

        state_update = {
            "budgets": updated_state.budgets,
            "implementation_done": result.status == "done",
            "task_status": "running" if result.status == "done" else "failed",
        }

        return NodeResult(
            next_node="VERIFY_FIX",
            state_update=state_update,
            status="ok" if result.status == "done" else "warning",
        )

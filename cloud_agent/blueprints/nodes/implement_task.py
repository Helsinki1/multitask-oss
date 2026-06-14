"""Node 04: IMPLEMENT_TASK (LLM Subsession).

The main implementation loop. Model inspects, edits, runs commands.
"""

from cloud_agent.agent.escalation import EscalationConfig
from cloud_agent.agent.prompts import build_implement_human, build_implement_system
from cloud_agent.agent.runtime import Node, NodeResult
from cloud_agent.agent.state import AgentState
from cloud_agent.agent.subsession import SubsessionConfig, run_subsession
from cloud_agent.config import settings
from cloud_agent.observability.tracer import Tracer
from cloud_agent.tools.registry import ToolRegistry, build_dev_toolset


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

        config = SubsessionConfig(
            name="implement_task",
            system_prompt=build_implement_system(state, state.context_bundle),
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
            next_node="05_CHECKPOINT",
            state_update=state_update,
            status="ok" if result.status == "done" else "warning",
        )

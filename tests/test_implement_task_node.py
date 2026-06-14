from __future__ import annotations

from unittest.mock import MagicMock, patch

from cloud_agent.agent.state import AgentState
from cloud_agent.agent.subsession import SubsessionResult
from cloud_agent.blueprints.nodes.implement_task import ImplementTaskNode


def test_implement_task_failure_marks_task_failed():
    state = AgentState(workspace_path="/tmp/repo", task_text="do work")
    updated = state.apply_update({"budgets": state.budgets})
    node = ImplementTaskNode(tracer=MagicMock())

    with patch("cloud_agent.blueprints.nodes.implement_task.run_subsession") as run:
        run.return_value = (SubsessionResult(status="error"), updated)
        result = node.run(state)

    assert result.status == "warning"
    assert result.next_node == "05_CHECKPOINT"
    assert result.state_update["implementation_done"] is False
    assert result.state_update["task_status"] == "failed"


def test_implement_task_success_leaves_task_running_until_engine_finalizes():
    state = AgentState(workspace_path="/tmp/repo", task_text="do work")
    updated = state.apply_update({"budgets": state.budgets})
    node = ImplementTaskNode(tracer=MagicMock())

    with patch("cloud_agent.blueprints.nodes.implement_task.run_subsession") as run:
        run.return_value = (SubsessionResult(status="done"), updated)
        result = node.run(state)

    assert result.status == "ok"
    assert result.state_update["implementation_done"] is True
    assert result.state_update["task_status"] == "running"

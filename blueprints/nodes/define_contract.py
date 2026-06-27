"""Node DEFINE_CONTRACT: LLM writes a failing test file that defines required behavior.

The contract test file (_contract_tests.py) is the verifiable specification for
an additive task. It must:
  - Import the code that WILL be implemented
  - Assert specific, concrete behaviors (not trivially true)
  - Currently FAIL (the feature doesn't exist yet)

The node verifies the tests actually fail before proceeding to IMPLEMENT.
If they somehow pass (the feature already exists), it skips straight to CHECKPOINT.
"""

from __future__ import annotations

import os

from agent.prompts import build_define_contract_human, build_define_contract_system
from agent.runtime import Node, NodeResult
from agent.state import AgentState, TestCase, TestToDoList
from agent.subsession import SubsessionConfig, run_subsession
from cloud_agent.config import settings
from observability.tracer import Tracer
from tools.registry import ToolRegistry, build_dev_toolset
from tools.shell import run_cmd

_CONTRACT_FILE = "_contract_tests.py"
_MAX_RETRIES = 2


class DefineContractNode(Node):
    name = "DEFINE_CONTRACT"
    node_type = "llm_subsession"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        registry = ToolRegistry()
        build_dev_toolset(registry)

        config = SubsessionConfig(
            name="define_contract",
            system_prompt=build_define_contract_system(),
            initial_human_message=build_define_contract_human(state),
            model=settings.implement_model,
            tools_schema=registry.to_openai_schema(),
            max_turns=20,
            max_wall_seconds=600,
            max_tokens=4096,
        )

        result, updated_state = run_subsession(config, state, registry, self.tracer)

        self.tracer.emit("subsession.done", {
            "name": "define_contract",
            "status": result.status,
            "turns": result.total_turns,
        })

        contract_path = os.path.join(state.workspace_path, _CONTRACT_FILE)
        if not os.path.isfile(contract_path):
            raise RuntimeError(
                f"DEFINE_CONTRACT subsession ended without creating {_CONTRACT_FILE}"
            )

        # Verify the contract tests actually fail (feature not yet implemented)
        rc, out = run_cmd(
            f"python -m pytest {_CONTRACT_FILE} --tb=short -q --no-header",
            state.workspace_path,
            timeout=120,
        )

        self.tracer.emit("define_contract.verification", {
            "exit_code": rc,
            "contract_file": _CONTRACT_FILE,
        })

        if rc == 0:
            # Tests pass already — feature may already be implemented, skip to CHECKPOINT
            self.tracer.emit("define_contract.already_passing", {})
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={"budgets": updated_state.budgets, "contract_test_path": _CONTRACT_FILE},
                status="ok",
            )

        # Tests fail as expected — build todo_list from contract tests
        rc2, list_out = run_cmd(
            f"python -m pytest {_CONTRACT_FILE} --collect-only -q --no-header",
            state.workspace_path,
            timeout=30,
        )
        test_ids = _parse_collected_ids(list_out, state.workspace_path)
        if not test_ids:
            # Fallback: treat the whole file as one test ID
            test_ids = [_CONTRACT_FILE]

        cases = [
            TestCase(test_id=tid, category="fail_to_pass", status="failing", traceback=out)
            for tid in test_ids
        ]
        todo_list = TestToDoList(cases=cases)

        return NodeResult(
            next_node="IMPLEMENT",
            state_update={
                "budgets": updated_state.budgets,
                "contract_test_path": _CONTRACT_FILE,
                "todo_list": todo_list,
            },
            status="ok",
        )


def _parse_collected_ids(output: str, workspace: str) -> list[str]:
    """Parse pytest --collect-only output to get test IDs."""
    ids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith("=") and not line.startswith("<"):
            ids.append(line)
    return ids

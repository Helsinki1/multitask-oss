"""Node VERIFY_ADDITIVE: deterministic gate for the additive task path.

Runs the contract tests written by DEFINE_CONTRACT.

Routing:
  - All pass  → CHECKPOINT
  - Failing   → DEFINE_CONTRACT (re-examine desired behavior, refine contract + implement)
  - Max retries → CHECKPOINT
"""

from __future__ import annotations

from agent.runtime import Node, NodeResult
from agent.state import AgentState, TestCase, TestToDoList
from observability.tracer import Tracer
from tools.shell import run_cmd

MAX_VERIFY_ATTEMPTS = 3
_TEST_TIMEOUT = 120


class VerifyAdditiveNode(Node):
    name = "VERIFY_ADDITIVE"
    node_type = "deterministic"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path
        contract = state.contract_test_path or "_contract_tests.py"

        rc, out = run_cmd(
            f"python -m pytest {contract} --tb=long --no-header -q",
            ws,
            timeout=_TEST_TIMEOUT,
        )

        self.tracer.emit("verify_additive.result", {
            "contract": contract,
            "passed": rc == 0,
            "exit_code": rc,
        })

        if rc == 0:
            self.tracer.emit("verify_additive.passed", {})
            # Update todo_list to reflect all passing
            updated = TestToDoList(cases=[
                TestCase(test_id=c.test_id, category=c.category, status="passing", traceback="")
                for c in state.todo_list.cases
            ])
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={"todo_list": updated},
                status="ok",
            )

        new_attempt = state.verify_attempts + 1
        if new_attempt >= MAX_VERIFY_ATTEMPTS:
            self.tracer.emit("verify_additive.exhausted", {"attempts": new_attempt})
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={"verify_attempts": new_attempt},
                status="warning",
            )

        # Update todo_list with current failure tracebacks
        updated_cases = [
            TestCase(test_id=c.test_id, category=c.category, status="failing", traceback=out)
            for c in state.todo_list.cases
        ]
        updated_todo = TestToDoList(cases=updated_cases)

        self.tracer.emit("verify_additive.retry", {"attempt": new_attempt})
        return NodeResult(
            next_node="DEFINE_CONTRACT",
            state_update={
                "todo_list": updated_todo,
                "verify_attempts": new_attempt,
                "verify_failure_type": "f2p_failing",
            },
            status="warning",
        )

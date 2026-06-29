"""Node VERIFY: deterministic post-implementation gate for bug fix path.

Runs each fail_to_pass test individually and all pass_to_pass tests as a batch.
Updates the TestToDoList with current statuses and tracebacks.

Routing (separate edges for separate failure types):
  - All tests pass                 → CHECKPOINT
  - fail_to_pass still failing     → IMPLEMENT  (fix is wrong, re-implement)
  - pass_to_pass regression        → GATHER_CONTEXT  (fix broke something, re-derive context)
  - Max retries exhausted          → CHECKPOINT  (submit best attempt, don't deadlock)
"""

from __future__ import annotations

from agent.context import resolve_test_id
from agent.runtime import Node, NodeResult
from agent.state import AgentState, TestCase, TestToDoList
from observability.tracer import Tracer
from tools.shell import run_cmd

MAX_VERIFY_ATTEMPTS = 5
_TEST_TIMEOUT = 300


class VerifyNode(Node):
    name = "VERIFY"
    node_type = "deterministic"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path
        updated_cases = _run_all_tests(ws, state.todo_list, self.tracer)
        updated_todo = TestToDoList(cases=updated_cases)

        f2p_failing = updated_todo.f2p_failing
        baseline_set = set(state.baseline_p2p_failing)
        # Newly-broken: agent caused these (triggers regression routing → GATHER_CONTEXT)
        newly_failing = [c for c in updated_todo.p2p_failing if c.test_id not in baseline_set]
        # Baseline-still-failing: were broken before agent started; agent must also fix them
        # but they are NOT regressions (don't trigger GATHER_CONTEXT)
        baseline_still_failing = [c for c in updated_todo.p2p_failing if c.test_id in baseline_set]

        self.tracer.emit("verify.result", {
            "summary": updated_todo.summary(),
            "f2p_failing": [c.test_id for c in f2p_failing],
            "p2p_newly_failing": [c.test_id for c in newly_failing],
            # Informational only — baseline failures are pre-existing env issues, not regressions.
            # They do NOT block success and do NOT trigger IMPLEMENT loops.
            "p2p_baseline_still_failing": len(baseline_still_failing),
        })

        # Full success: f2p pass and no new regressions.
        # Baseline-failing tests are pre-existing env failures (e.g. Python version compat) —
        # requiring the agent to fix them causes unrelated edits that introduce real regressions.
        if not f2p_failing and not newly_failing:
            self.tracer.emit("verify.passed", {})
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={"todo_list": updated_todo, "verify_failure_type": ""},
                status="ok",
            )

        new_attempt = state.verify_attempts + 1
        if new_attempt >= MAX_VERIFY_ATTEMPTS:
            self.tracer.emit("verify.exhausted", {"attempts": new_attempt})
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={
                    "todo_list": updated_todo,
                    "verify_attempts": new_attempt,
                    "verify_failure_type": _failure_type(f2p_failing, newly_failing),
                },
                status="warning",
            )

        # Routing priority:
        # 1. New regressions (agent broke something) → GATHER_CONTEXT for regression context
        # 2. f2p still failing OR baseline tests still failing → IMPLEMENT (fix is incomplete)
        if newly_failing:
            failure_type = "p2p_regression"
            next_node = "GATHER_CONTEXT"
        elif f2p_failing:
            failure_type = "f2p_failing"
            next_node = "IMPLEMENT"
        else:
            failure_type = ""
            next_node = "CHECKPOINT"

        self.tracer.emit("verify.retry", {
            "attempt": new_attempt,
            "failure_type": failure_type,
            "next_node": next_node,
        })

        return NodeResult(
            next_node=next_node,
            state_update={
                "todo_list": updated_todo,
                "verify_attempts": new_attempt,
                "verify_failure_type": failure_type,
            },
            status="warning",
        )


def _run_all_tests(workspace: str, todo_list: TestToDoList, tracer: Tracer) -> list[TestCase]:
    updated: list[TestCase] = []

    for case in todo_list.cases:
        test_id = resolve_test_id(workspace, case.test_id)
        if case.category == "fail_to_pass":
            rc, out = run_cmd(
                f"python -m pytest {test_id} -x --tb=long --no-header -q",
                workspace,
                timeout=_TEST_TIMEOUT,
            )
            status = "passing" if rc == 0 else "failing"
            tracer.emit("verify.test", {
                "test_id": case.test_id,
                "category": "fail_to_pass",
                "passed": rc == 0,
            })
            updated.append(TestCase(
                test_id=case.test_id,
                category="fail_to_pass",
                status=status,
                traceback=out if rc != 0 else "",
            ))
        else:
            # pass_to_pass: run with --tb=short, no -x (run all, catch all regressions)
            rc, out = run_cmd(
                f"python -m pytest {test_id} --tb=short --no-header -q",
                workspace,
                timeout=_TEST_TIMEOUT,
            )
            status = "passing" if rc == 0 else "failing"
            tracer.emit("verify.test", {
                "test_id": case.test_id,
                "category": "pass_to_pass",
                "passed": rc == 0,
            })
            updated.append(TestCase(
                test_id=case.test_id,
                category="pass_to_pass",
                status=status,
                traceback=out if rc != 0 else "",
            ))

    return updated


def _failure_type(f2p_failing: list, p2p_failing: list) -> str:
    if f2p_failing:
        return "f2p_failing"
    if p2p_failing:
        return "p2p_regression"
    return ""

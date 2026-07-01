"""Node VERIFY: deterministic post-implementation gate for bug fix path.

Runs each fail_to_pass test individually and all pass_to_pass tests as a batch.
Updates the TestToDoList with current statuses and tracebacks.

Routing (separate edges for separate failure types):
  - All tests pass                          → CHECKPOINT
  - fail_to_pass still failing, same error  → IMPLEMENT  (fix is wrong, re-implement)
  - fail_to_pass failing, error signature
    changed since last attempt              → GATHER_CONTEXT (targeted re-investigation,
                                                capped — see MAX_RECONTEXT_ATTEMPTS)
  - pass_to_pass regression                 → GATHER_CONTEXT (fix broke something,
                                                capped — see MAX_RECONTEXT_ATTEMPTS)
  - Recontext cap hit                       → IMPLEMENT  (plain retry, no fresh context)
  - Max verify attempts exhausted           → CHECKPOINT  (submit best attempt, don't deadlock)
"""

from __future__ import annotations

import re

from agent.context import resolve_test_id
from agent.runtime import Node, NodeResult
from agent.state import AgentState, TestCase, TestToDoList
from observability.tracer import Tracer
from tools.shell import run_cmd

MAX_VERIFY_ATTEMPTS = 5
MAX_RECONTEXT_ATTEMPTS = 3  # separate, smaller budget than MAX_VERIFY_ATTEMPTS — see agent/state.py
_TEST_TIMEOUT = 300


class VerifyNode(Node):
    name = "VERIFY"
    node_type = "deterministic"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path
        prior_by_id = {c.test_id: c for c in state.todo_list.cases}

        updated_cases = _run_all_tests(ws, state.todo_list, self.tracer)
        updated_todo = TestToDoList(cases=updated_cases)

        f2p_failing = updated_todo.f2p_failing
        baseline_set = set(state.baseline_p2p_failing)
        # Newly-broken: agent caused these (triggers regression routing → GATHER_CONTEXT)
        newly_failing = [c for c in updated_todo.p2p_failing if c.test_id not in baseline_set]
        # Baseline-still-failing: were broken before agent started; agent must also fix them
        # but they are NOT regressions (don't trigger GATHER_CONTEXT)
        baseline_still_failing = [c for c in updated_todo.p2p_failing if c.test_id in baseline_set]

        f2p_new_error = _signature_changed(f2p_failing, prior_by_id)

        self.tracer.emit("verify.result", {
            "summary": updated_todo.summary(),
            "f2p_failing": [c.test_id for c in f2p_failing],
            "p2p_newly_failing": [c.test_id for c in newly_failing],
            "f2p_new_error": f2p_new_error,
            # Informational only — baseline failures are pre-existing env issues, not regressions.
            # They do NOT block success and do NOT trigger IMPLEMENT loops.
            "p2p_baseline_still_failing": len(baseline_still_failing),
            "p2p_baseline_still_failing_ids": [c.test_id for c in baseline_still_failing],
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

        # Priority: p2p regression > f2p error-signature change > plain f2p retry
        if newly_failing:
            failure_type = "p2p_regression"
            reason = _p2p_reason(newly_failing)
        elif f2p_failing and f2p_new_error:
            failure_type = "f2p_new_error"
            reason = _f2p_new_error_reason(f2p_failing, prior_by_id)
        elif f2p_failing:
            failure_type = "f2p_failing"
            reason = ""
        else:
            failure_type = ""
            reason = ""

        new_attempt = state.verify_attempts + 1
        if new_attempt >= MAX_VERIFY_ATTEMPTS:
            self.tracer.emit("verify.exhausted", {"attempts": new_attempt})
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={
                    "todo_list": updated_todo,
                    "verify_attempts": new_attempt,
                    "verify_failure_type": failure_type,
                },
                status="warning",
            )

        wants_gather = failure_type in ("p2p_regression", "f2p_new_error")
        new_recontext_attempts = state.recontext_attempts
        if wants_gather and state.recontext_attempts < MAX_RECONTEXT_ATTEMPTS:
            next_node = "GATHER_CONTEXT"
            new_recontext_attempts = state.recontext_attempts + 1
        elif failure_type:
            # Plain f2p retry, or a gather-worthy failure that's hit the recontext cap —
            # fall back to a plain IMPLEMENT retry. IMPLEMENT still sees the failure via
            # its own prompt branching on verify_failure_type; it just won't get a fresh
            # GATHER_CONTEXT pass.
            next_node = "IMPLEMENT"
        else:
            next_node = "CHECKPOINT"

        self.tracer.emit("verify.retry", {
            "attempt": new_attempt,
            "failure_type": failure_type,
            "next_node": next_node,
            "recontext_attempts": new_recontext_attempts,
            "recontext_capped": wants_gather and next_node != "GATHER_CONTEXT",
        })

        return NodeResult(
            next_node=next_node,
            state_update={
                "todo_list": updated_todo,
                "verify_attempts": new_attempt,
                "verify_failure_type": failure_type,
                "recontext_attempts": new_recontext_attempts,
                "recontext_reason": reason if next_node == "GATHER_CONTEXT" else "",
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


def _exception_signature(traceback: str) -> str:
    """Extract the leading ExceptionType token from the last non-empty traceback line.

    E.g. "E   TypeError: unsupported operand" -> "TypeError". Returns "" if no
    traceback or no recognizable exception line (e.g. plain assert failure text).
    """
    if not traceback:
        return ""
    for line in reversed(traceback.strip().splitlines()):
        line = line.strip().lstrip("E").strip()
        if not line:
            continue
        m = re.match(r"^(\w+(?:\.\w+)*(?:Error|Exception|Warning))\b", line)
        return m.group(1) if m else ""
    return ""


def _signature_changed(f2p_failing: list, prior_by_id: dict) -> bool:
    """True if any still-failing f2p test's exception signature differs from its
    signature on the previous VERIFY pass (both signatures must be non-empty)."""
    for c in f2p_failing:
        prior = prior_by_id.get(c.test_id)
        if prior is None:
            continue
        old_sig = _exception_signature(prior.traceback)
        new_sig = _exception_signature(c.traceback)
        if old_sig and new_sig and old_sig != new_sig:
            return True
    return False


def _p2p_reason(newly_failing: list) -> str:
    ids = [c.test_id for c in newly_failing]
    shown = ", ".join(ids[:5])
    more = f" (+{len(ids) - 5} more)" if len(ids) > 5 else ""
    return (
        f"Your fix introduced {len(ids)} pass_to_pass regression(s): {shown}{more}. "
        "Investigate why and find a fix that doesn't break them."
    )


def _f2p_new_error_reason(f2p_failing: list, prior_by_id: dict) -> str:
    parts = []
    for c in f2p_failing:
        prior = prior_by_id.get(c.test_id)
        if prior is None:
            continue
        old_sig = _exception_signature(prior.traceback)
        new_sig = _exception_signature(c.traceback)
        if old_sig and new_sig and old_sig != new_sig:
            parts.append(f"{c.test_id}: {old_sig} -> {new_sig}")
    detail = "; ".join(parts) if parts else "the failure signature changed"
    return (
        f"A fail_to_pass test's error changed since the last attempt ({detail}) — "
        "investigate the new failure mode rather than repeating the same fix."
    )

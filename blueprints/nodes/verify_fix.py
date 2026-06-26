"""Node VERIFY_FIX: deterministic post-implementation gate.

Runs checks after IMPLEMENT_TASK:
  - Normal mode (bug_fix): _repro_env/run_repro.py must exit 0, test suite must pass.
  - Normal mode (additive): test suite must pass (no repro script).
  - Eval mode (SWE-bench etc.): FAIL_TO_PASS tests must now pass; PASS_TO_PASS must not regress.

On pass  → routes to 05_CHECKPOINT.
On fail  → routes back to 04_IMPLEMENT_TASK with failure output in state,
           up to MAX_VERIFY_ATTEMPTS times. After that routes to 05_CHECKPOINT
           to avoid deadlock on unfixable failures.
"""

from __future__ import annotations

import pathlib

from cloud_agent.agent.runtime import Node, NodeResult
from cloud_agent.agent.state import AgentState
from cloud_agent.observability.tracer import Tracer
from cloud_agent.tools.shell import run_cmd

MAX_VERIFY_ATTEMPTS = 3
_REPRO_CANDIDATES = ("_repro_env/run_repro.py", "_repro_test.py")
_TEST_TIMEOUT = 300  # pytest on large repos can be slow


def _find_repro(workspace: str) -> pathlib.Path | None:
    for rel in _REPRO_CANDIDATES:
        p = pathlib.Path(workspace) / rel
        if p.exists():
            return p
    return None


class VerifyFixNode(Node):
    name = "VERIFY_FIX"
    node_type = "deterministic"
    failure_next = "05_CHECKPOINT"  # if this node itself crashes, skip to checkpoint

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path
        failures = self._check_eval_mode(state, ws) if state.eval_mode else self._check_normal(state, ws)

        if not failures:
            self.tracer.emit("verify.passed", {"attempt": state.verify_attempts + 1})
            return NodeResult(
                next_node="05_CHECKPOINT",
                state_update={"test_status": "passed"},
                status="ok",
            )

        new_attempt = state.verify_attempts + 1
        failure_summary = "\n\n---\n\n".join(failures)
        self.tracer.emit("verify.failed", {
            "attempt": new_attempt,
            "max_attempts": MAX_VERIFY_ATTEMPTS,
            "preview": failure_summary[:400],
        })

        if new_attempt >= MAX_VERIFY_ATTEMPTS:
            self.tracer.emit("verify.exhausted", {"attempts": new_attempt})
            return NodeResult(
                next_node="05_CHECKPOINT",
                state_update={
                    "test_status": "failed",
                    "verify_attempts": new_attempt,
                    "verify_failure_output": failure_summary,
                },
                status="warning",
            )

        return NodeResult(
            next_node="04_IMPLEMENT_TASK",
            state_update={
                "test_status": "failed",
                "verify_attempts": new_attempt,
                "verify_failure_output": failure_summary,
            },
            status="warning",
        )

    def _check_normal(self, state: AgentState, ws: str) -> list[str]:
        failures: list[str] = []

        # Repro check — only for bug_fix tasks; additive tasks have no repro script
        if state.task_type != "additive":
            repro_script = _find_repro(ws)
            if repro_script is not None:
                rc, out = run_cmd(f"python3 {repro_script}", ws)
                passed = rc == 0
                self.tracer.emit("verify.repro", {
                    "script": str(repro_script),
                    "exit_code": rc,
                    "passed": passed,
                })
                if not passed:
                    failures.append(
                        f"Repro script still exits non-zero — fix did not take:\n"
                        f"$ python3 {repro_script.relative_to(ws)}\n"
                        f"{out[:1200]}"
                    )
            else:
                self.tracer.emit("verify.repro", {"skipped": True, "reason": "no repro script found"})
        else:
            self.tracer.emit("verify.repro", {"skipped": True, "reason": "additive task"})

        # Test suite check — stop on first failing command
        test_commands = getattr(state.context_bundle, "build_and_test_commands", [])
        for cmd in test_commands:
            rc, out = run_cmd(cmd, ws, timeout=_TEST_TIMEOUT)
            passed = rc == 0
            self.tracer.emit("verify.tests", {"cmd": cmd, "exit_code": rc, "passed": passed})
            if not passed:
                failures.append(f"Test suite failed:\n$ {cmd}\n{out[:1500]}")
                break

        return failures

    def _check_eval_mode(self, state: AgentState, ws: str) -> list[str]:
        """In eval_mode: FAIL_TO_PASS must now pass; PASS_TO_PASS must not regress."""
        failures: list[str] = []

        if state.fail_to_pass:
            test_ids = " ".join(state.fail_to_pass)
            rc, out = run_cmd(
                f"python3 -m pytest {test_ids} -x --tb=short",
                ws, timeout=_TEST_TIMEOUT,
            )
            self.tracer.emit("verify.eval.fail_to_pass", {
                "exit_code": rc,
                "tests": state.fail_to_pass,
                "passed": rc == 0,
            })
            if rc != 0:
                failures.append(f"FAIL_TO_PASS tests still failing:\n{out[:1500]}")

        if state.pass_to_pass:
            test_ids = " ".join(state.pass_to_pass)
            rc, out = run_cmd(
                f"python3 -m pytest {test_ids} --tb=short",
                ws, timeout=_TEST_TIMEOUT,
            )
            self.tracer.emit("verify.eval.pass_to_pass", {
                "exit_code": rc,
                "tests_count": len(state.pass_to_pass),
                "passed": rc == 0,
            })
            if rc != 0:
                failures.append(f"PASS_TO_PASS regression:\n{out[:1500]}")

        return failures

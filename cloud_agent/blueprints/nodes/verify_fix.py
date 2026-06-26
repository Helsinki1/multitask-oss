"""Node VERIFY_FIX: deterministic post-implementation gate.

Runs two checks after IMPLEMENT_TASK:
  1. _repro_env/run_repro.py (or _repro_test.py) must exit 0 — bug is confirmed fixed.
  2. Detected test suite commands must all pass — no regressions introduced.

On pass  → routes to 05_CHECKPOINT.
On fail  → routes back to 04_IMPLEMENT_TASK with failure output written into state,
           up to MAX_VERIFY_ATTEMPTS times. After that the gate opens and routes to
           05_CHECKPOINT anyway to avoid deadlock on unfixable failures.
"""

from __future__ import annotations

import pathlib
import subprocess

from cloud_agent.agent.runtime import Node, NodeResult
from cloud_agent.agent.state import AgentState
from cloud_agent.observability.tracer import Tracer
from cloud_agent.tools.shell import _CLEAN_ENV

MAX_VERIFY_ATTEMPTS = 3
_REPRO_CANDIDATES = ("_repro_env/run_repro.py", "_repro_test.py")
_TIMEOUT = 120


def _run(cmd: str, cwd: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_TIMEOUT,
            env=_CLEAN_ENV,
        )
        return r.returncode, (r.stdout + "\n" + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {_TIMEOUT}s: {cmd}"
    except Exception as exc:
        return 1, f"error running command: {exc}"


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
        failures: list[str] = []

        # 1. Repro check — only runs if a repro script was produced by REPRODUCE_ISSUE
        repro_script = _find_repro(ws)
        if repro_script is not None:
            rc, out = _run(f"python3 {repro_script}", ws)
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

        # 2. Test suite check — stop on first failing command
        test_commands = getattr(state.context_bundle, "build_and_test_commands", [])
        for cmd in test_commands:
            rc, out = _run(cmd, ws)
            passed = rc == 0
            self.tracer.emit("verify.tests", {"cmd": cmd, "exit_code": rc, "passed": passed})
            if not passed:
                failures.append(
                    f"Test suite failed:\n$ {cmd}\n{out[:1500]}"
                )
                break

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

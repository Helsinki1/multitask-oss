"""Node REPRODUCE_ISSUE: focused subsession that builds a minimal mirror repo and
confirms the bug is reproducible with a practically identical error message before
any source code is touched.

The node enforces a hard gate: it will not advance to IMPLEMENT_TASK until:
  1. _repro_env/run_repro.py exists and exits non-zero, AND
  2. the error output shares key identifiers with the reported issue.

If two attempts fail to satisfy both conditions, the gate opens anyway with
repro_confirmed=False so the run is never deadlocked, but the failure is logged.

In eval_mode (SWE-bench etc.) the LLM subsession is skipped entirely: the harness
has already applied the failing tests via test_patch, so this node just confirms
they still fail before any source is touched.
"""

from __future__ import annotations

import re
import pathlib
from typing import Optional

from agent.runtime import Node, NodeResult
from agent.state import AgentState
from agent.subsession import SubsessionConfig, run_subsession
from cloud_agent.config import settings
from observability.tracer import Tracer
from tools.registry import ToolRegistry, build_dev_toolset
from tools.shell import run_cmd

_REPRO_SCRIPT = "_repro_env/run_repro.py"
_REPRO_FALLBACK = "_repro_test.py"
_MAX_CORRECTION_TURNS = 5

_REPRO_SYSTEM = """\
You are in the REPRODUCE phase. Your only job: build a minimal mirror environment \
and confirm the reported bug is reproducible before any source code is touched.

Steps:
1. Analyze the task to identify the exact error type/message and which source files are involved.

2. Build _repro_env/ — a minimal mirror containing ONLY the files needed to trigger the error:
   - Copy relevant source files via run_shell("cp -r src/module _repro_env/module")
   - Include any __init__.py files needed for imports
   - Do NOT copy the entire repo — only files referenced in the error or stack trace

3. Write _repro_env/run_repro.py that imports from the mirror and triggers the bug.

4. Run it: run_shell("python _repro_env/run_repro.py")

5. Verify:
   - Exit code must be non-zero
   - The error message in the output must match the error described in the task
   - If the error is different or missing, revise the mirror files or the script

6. Stop only when the output is practically identical to the reported error.

Rules:
- Do NOT fix anything. Do NOT edit source files outside _repro_env/.
- _repro_env/ should be the smallest set of files that reproduces the bug.
- The script must be self-contained and runnable with: python _repro_env/run_repro.py\
"""


def _extract_error_tokens(text: str) -> set[str]:
    """Pull exception class names and short post-colon phrases from text."""
    tokens: set[str] = set()
    tokens.update(re.findall(r'\b(\w+(?:Error|Exception|Warning|Fault))\b', text))
    for match in re.finditer(r'\w+(?:Error|Exception)[:\s]+(.+?)(?:\n|$)', text):
        words = match.group(1).strip().split()[:4]
        if words:
            tokens.add(" ".join(words).lower())
    return tokens


def _repro_matches_issue(task_text: str, repro_output: str) -> bool:
    """Return True if repro_output shares key error identifiers with the task description."""
    issue_tokens = _extract_error_tokens(task_text)
    if not issue_tokens:
        return True
    repro_tokens = _extract_error_tokens(repro_output)
    repro_lower = repro_output.lower()
    exception_names = {t for t in issue_tokens if t[0].isupper()}
    if exception_names & repro_tokens:
        return True
    for token in issue_tokens - exception_names:
        if token in repro_lower:
            return True
    return False


def _find_repro_script(workspace: str) -> Optional[pathlib.Path]:
    primary = pathlib.Path(workspace) / _REPRO_SCRIPT
    if primary.exists():
        return primary
    fallback = pathlib.Path(workspace) / _REPRO_FALLBACK
    if fallback.exists():
        return fallback
    return None


def _verify(workspace: str, task_text: str) -> tuple[bool, str, str]:
    """Run the repro script and check it matches the issue.

    Returns (matched, script_path_str, output).
    matched=False if script missing, exits 0, or error doesn't match.
    """
    script = _find_repro_script(workspace)
    if script is None:
        return False, "", "No repro script found at _repro_env/run_repro.py or _repro_test.py"
    rc, output = run_cmd(f"python3 {script}", workspace, timeout=60)
    if rc == 0:
        return False, str(script), f"Repro exited 0 (expected non-zero):\n{output}"
    if not _repro_matches_issue(task_text, output):
        return False, str(script), output
    return True, str(script), output


class ReproduceIssueNode(Node):
    name = "REPRODUCE_ISSUE"
    node_type = "llm_subsession"
    failure_next = "04_IMPLEMENT_TASK"  # if the node itself crashes, proceed anyway

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        if state.eval_mode:
            return self._run_eval_mode(state)

        registry = ToolRegistry()
        build_dev_toolset(registry)

        # --- Attempt 1: main repro subsession ---
        config = SubsessionConfig(
            name="reproduce_issue",
            system_prompt=_REPRO_SYSTEM,
            initial_human_message=f"Task / issue:\n{state.task_text}",
            model=settings.discovery_model,
            tools_schema=registry.to_openai_schema(),
            max_turns=settings.repro_max_turns,
            max_tokens=8192,
        )
        _, state = run_subsession(config, state, registry, self.tracer)

        matched, script_path, output = _verify(state.workspace_path, state.task_text)

        self.tracer.emit("repro.verify", {
            "attempt": 1,
            "matched": matched,
            "script": script_path,
            "output_preview": output[:300],
        })

        if not matched:
            # --- Attempt 2: short correction subsession ---
            correction_msg = (
                f"Task / issue:\n{state.task_text}\n\n"
                f"Your repro attempt did not match the reported error.\n"
                f"Script run: {script_path or '(not found)'}\n"
                f"Output:\n{output[:600]}\n\n"
                "Revise _repro_env/run_repro.py (and the mirrored files if needed) so the "
                "output contains the same error type and message as the reported issue. "
                "Run the script again and confirm it fails with the right error."
            )
            correction_config = SubsessionConfig(
                name="reproduce_issue_correction",
                system_prompt=_REPRO_SYSTEM,
                initial_human_message=correction_msg,
                model=settings.discovery_model,
                tools_schema=registry.to_openai_schema(),
                max_turns=_MAX_CORRECTION_TURNS,
                max_tokens=8192,
            )
            _, state = run_subsession(correction_config, state, registry, self.tracer)

            matched, script_path, output = _verify(state.workspace_path, state.task_text)

            self.tracer.emit("repro.verify", {
                "attempt": 2,
                "matched": matched,
                "script": script_path,
                "output_preview": output[:300],
            })

        repro_confirmed = matched
        if not repro_confirmed:
            self.tracer.emit("repro.unconfirmed", {
                "reason": "error output did not match issue after 2 attempts",
                "last_output": output[:300],
            })

        return NodeResult(
            next_node="04_IMPLEMENT_TASK",
            state_update={
                "repro_confirmed": repro_confirmed,
                "repro_output": output,
                "repro_env_path": script_path,
            },
        )

    def _run_eval_mode(self, state: AgentState) -> NodeResult:
        """Deterministic repro confirmation for eval_mode: run FAIL_TO_PASS tests, expect non-zero."""
        if not state.fail_to_pass:
            self.tracer.emit("repro.eval_mode", {"skipped": True, "reason": "no fail_to_pass tests"})
            return NodeResult(
                next_node="04_IMPLEMENT_TASK",
                state_update={"repro_confirmed": False, "repro_output": "no FAIL_TO_PASS tests specified"},
            )

        test_ids = " ".join(state.fail_to_pass)
        rc, output = run_cmd(
            f"python3 -m pytest {test_ids} -x --tb=short",
            state.workspace_path,
            timeout=120,
        )
        confirmed = rc != 0

        self.tracer.emit("repro.eval_mode", {
            "fail_to_pass": state.fail_to_pass,
            "exit_code": rc,
            "confirmed": confirmed,
            "output_preview": output[:300],
        })

        if not confirmed:
            self.tracer.emit("repro.unconfirmed", {
                "reason": "FAIL_TO_PASS tests already pass — check that test_patch was applied",
                "output": output[:300],
            })

        return NodeResult(
            next_node="04_IMPLEMENT_TASK",
            state_update={
                "repro_confirmed": confirmed,
                "repro_output": output,
            },
        )

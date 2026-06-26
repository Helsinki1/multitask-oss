"""Deterministic model escalation policy for run_subsession."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


_BLOCK_PHRASES = (
    "unable to proceed",
    "i'm unable to",
    "i am unable to",
    "cannot proceed",
    "i'm stuck",
    "i am stuck",
    "i don't know how to",
    "i do not know how to",
    "cannot complete this",
    "no way to",
    "i've exhausted",
    "i have exhausted",
    "i cannot continue",
)


@dataclass
class EscalationConfig:
    default_model: str
    escalated_model: str
    escalation_enabled: bool = True
    max_escalations: int = 1
    escalation_after_failed_completion_checks: int = 2
    escalation_after_repeated_tool_failures: int = 2
    escalation_after_turn_fraction: float = 0.4


@dataclass
class EscalationMetrics:
    current_model: str = ""
    escalation_count: int = 0
    failed_completion_checks: int = 0
    repeated_tool_failure_count: int = 0
    escalation_reason: str = ""
    _last_failing_key: str = field(default="", repr=False)
    _consecutive_same_failure: int = field(default=0, repr=False)


def should_escalate(
    config: EscalationConfig,
    metrics: EscalationMetrics,
    turn: int,
    max_turns: int,
    last_text: str = "",
) -> tuple[bool, str]:
    """Pure function. Returns (should_escalate, reason). No side effects."""
    if not config.escalation_enabled:
        return False, ""
    if metrics.escalation_count >= config.max_escalations:
        return False, ""
    if config.default_model == config.escalated_model:
        return False, ""

    if metrics.failed_completion_checks >= config.escalation_after_failed_completion_checks:
        return True, f"failed completion checks: {metrics.failed_completion_checks}"

    if metrics.repeated_tool_failure_count >= config.escalation_after_repeated_tool_failures:
        return True, f"repeated tool failures: {metrics.repeated_tool_failure_count}"

    if (
        config.escalation_after_turn_fraction > 0
        and turn >= int(max_turns * config.escalation_after_turn_fraction)
    ):
        return True, f"turn threshold reached: turn {turn}/{max_turns}"

    if last_text and _is_blocked_text(last_text):
        return True, "model reported being blocked or unable to proceed"

    return False, ""


def _is_blocked_text(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _BLOCK_PHRASES)


def record_tool_result(
    metrics: EscalationMetrics,
    tool_name: str,
    args: dict,
    result: str,
) -> None:
    """Update metrics.repeated_tool_failure_count based on this tool execution result."""
    key_arg = ""
    for k in ("command", "cmd", "path", "file_path", "query"):
        if k in args:
            key_arg = str(args[k])[:80]
            break
    failing_key = f"{tool_name}:{key_arg}"

    if _is_tool_failure(result):
        if failing_key == metrics._last_failing_key:
            metrics._consecutive_same_failure += 1
        else:
            metrics._last_failing_key = failing_key
            metrics._consecutive_same_failure = 1
        metrics.repeated_tool_failure_count = metrics._consecutive_same_failure
    else:
        metrics._last_failing_key = ""
        metrics._consecutive_same_failure = 0
        metrics.repeated_tool_failure_count = 0


def _is_tool_failure(result: str) -> bool:
    """Detect non-zero exit codes (run_shell/run_tests format) and Python tracebacks."""
    m = re.search(r"exit code:\s*(\d+)", result)
    if m and m.group(1) != "0":
        return True
    if "Traceback (most recent call last)" in result:
        return True
    return False


def build_escalation_message(
    task_text: str,
    reason: str,
    turn: int,
    cost_so_far: float,
    messages: list[dict],
    escalated_model: str,
) -> str:
    """Build the developer context message injected into the conversation on escalation."""
    files_written: list[str] = []
    commands_run: list[str] = []
    recent_failures: list[str] = []

    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except Exception:
                    args = {}
                if name in ("write_file", "apply_patch", "replace_in_file"):
                    path = (
                        args.get("path")
                        or args.get("file_path")
                        or args.get("target_file", "")
                    )
                    if path:
                        files_written.append(str(path))
                elif name in ("run_shell", "run_tests"):
                    cmd = args.get("command", "")
                    if cmd:
                        commands_run.append(str(cmd))
        elif msg.get("role") == "tool":
            content = msg.get("content", "")
            if _is_tool_failure(content):
                recent_failures.append(content[:200])

    unique_files = list(dict.fromkeys(files_written))
    unique_cmds = list(dict.fromkeys(commands_run))
    last_failures = recent_failures[-3:]

    files_str = ", ".join(unique_files) if unique_files else "none"
    cmds_str = ", ".join(unique_cmds) if unique_cmds else "none"
    failures_str = "\n---\n".join(last_failures) if last_failures else "none"

    return (
        f"[ESCALATION — switching to {escalated_model}]\n"
        f"Reason: {reason}\n"
        f"Turn: {turn} | Cost so far: ${cost_so_far:.4f}\n\n"
        f"Task: {task_text[:300]}\n\n"
        f"Files modified so far: {files_str}\n"
        f"Commands attempted: {cmds_str}\n\n"
        f"Recent failures:\n{failures_str}\n\n"
        f"You are a more capable model taking over. Review the above context, "
        f"understand what was tried, and continue from where the previous model left off. "
        f"Focus on resolving the blocking issue identified in the reason above."
    )

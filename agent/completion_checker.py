"""IsDoneChecker: structured completion check via a small OpenAI call."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import openai

from agent.prompts import IS_DONE_SYSTEM
from agent.state import AgentState
from cloud_agent.config import settings


@dataclass
class IsDoneOutput:
    is_done: bool
    confidence: str  # "low" | "medium" | "high"
    reason: str
    missing_steps: list[str] = field(default_factory=list)


_CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "check_completion",
        "description": "Report whether the coding task is complete with structured evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "is_done": {
                    "type": "boolean",
                    "description": "True only if there is concrete evidence the task is complete.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Confidence level in the is_done assessment.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why the task is or isn't complete.",
                },
                "missing_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Steps still needed to complete the task (empty if done).",
                },
            },
            "required": ["is_done", "confidence", "reason", "missing_steps"],
        },
    },
}


def _get_git_diff_summary(workspace: str) -> str:
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--stat"],
            capture_output=True, text=True, cwd=workspace,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, cwd=workspace,
        ).stdout.strip()
        combined = "\n".join(filter(None, [diff, status]))
        return combined or "(no diff)"
    except Exception:
        return "(git unavailable)"


def check_is_done(
    task_text: str,
    last_assistant_message: str,
    state: AgentState,
    max_retries: int = 2,
) -> IsDoneOutput:
    diff_summary = _get_git_diff_summary(state.workspace_path)

    user_msg = f"""Task: {task_text}

Last agent message:
{last_assistant_message[:2000]}

Git diff summary (files changed):
{diff_summary}

Test status: {state.test_status}
Lint status: {state.lint_status}

Is the task complete? Call check_completion with your assessment."""

    client = openai.OpenAI(api_key=settings.openai_api_key)

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=settings.checker_model,
                max_tokens=512,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": IS_DONE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                tools=[_CHECK_TOOL],
                tool_choice={"type": "function", "function": {"name": "check_completion"}},
            )
            msg = response.choices[0].message
            if msg.tool_calls:
                inp = json.loads(msg.tool_calls[0].function.arguments)
                return IsDoneOutput(
                    is_done=bool(inp.get("is_done", False)),
                    confidence=inp.get("confidence", "low"),
                    reason=inp.get("reason", ""),
                    missing_steps=inp.get("missing_steps", []),
                )
        except Exception:
            if attempt == max_retries:
                break

    # Fallback heuristic: if diff exists, assume done
    has_diff = diff_summary not in ("(no diff)", "(git unavailable)", "")
    return IsDoneOutput(
        is_done=has_diff,
        confidence="low",
        reason="Checker failed — falling back to heuristic (diff exists = done)",
        missing_steps=[] if has_diff else ["Make code changes", "Verify changes work"],
    )

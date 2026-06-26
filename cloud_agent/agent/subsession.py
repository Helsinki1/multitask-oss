"""LLM subsession: the core agent loop (tool-call cycle + done-checking)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import openai

from cloud_agent.agent.budgets import BudgetExhausted, check_budget, estimate_cost
from cloud_agent.agent.completion_checker import IsDoneOutput, check_is_done
from cloud_agent.agent.escalation import (
    EscalationConfig,
    EscalationMetrics,
    build_escalation_message,
    record_tool_result,
    should_escalate,
)
from cloud_agent.agent.prompts import build_nudge
from cloud_agent.agent.state import AgentState
from cloud_agent.config import settings
from cloud_agent.observability.tracer import Tracer
from cloud_agent.tools.registry import ToolRegistry


@dataclass
class SubsessionConfig:
    name: str
    system_prompt: str
    initial_human_message: str
    model: str
    tools_schema: list[dict] = field(default_factory=list)
    max_turns: int = 100
    max_wall_seconds: int = 7200
    max_tokens: int = 8192
    escalation_config: EscalationConfig | None = None


@dataclass
class SubsessionResult:
    status: str  # "done" | "budget_exhausted" | "error"
    messages: list[dict] = field(default_factory=list)
    summary: str = ""
    total_turns: int = 0
    total_cost_usd: float = 0.0


_COMPRESS_AFTER_TURN = 20
_KEEP_RECENT_TURNS = 6


def _token_limit_kwargs(model: str, max_tokens: int) -> dict[str, int]:
    """Return the token limit parameter supported by the selected model family."""
    if model.startswith("gpt-5") or model.startswith("codex-"):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _compress_history(messages: list[dict], keep_turns: int = 6) -> None:
    """Truncate tool message content for turns older than keep_turns. Mutates in place.

    Walks backward counting assistant-with-tool-calls as turn boundaries.
    Tool messages beyond keep_turns are collapsed to 120 chars so the model
    retains a breadcrumb of what ran without burning context on stale output.
    The tool_call_id chain is preserved so OpenAI's message structure stays valid.
    """
    turns_seen = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            turns_seen += 1
        if turns_seen > keep_turns and msg["role"] == "tool":
            content = msg.get("content", "")
            if len(content) > 120:
                msg["content"] = content[:120] + f" … [compressed, was {len(content)} chars]"


def run_subsession(
    config: SubsessionConfig,
    state: AgentState,
    registry: ToolRegistry,
    tracer: Tracer,
) -> tuple[SubsessionResult, AgentState]:
    """Run one LLM subsession. Returns (result, updated_state)."""
    client = openai.OpenAI(api_key=settings.openai_api_key)

    messages: list[dict] = [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": config.initial_human_message},
    ]
    start_time = time.time()
    total_cost = 0.0
    last_text = ""

    # Escalation state — only active when escalation_config is provided
    esc = config.escalation_config
    metrics: EscalationMetrics | None = EscalationMetrics(current_model=config.model) if esc else None
    current_model = config.model

    def _maybe_escalate(turn: int, check_text: str = "") -> None:
        nonlocal current_model
        if esc is None or metrics is None:
            return
        triggered, reason = should_escalate(esc, metrics, turn, config.max_turns, check_text)
        if not triggered:
            return
        esc_msg = build_escalation_message(
            task_text=state.task_text,
            reason=reason,
            turn=turn,
            cost_so_far=total_cost,
            messages=messages,
            escalated_model=esc.escalated_model,
        )
        messages.append({"role": "user", "content": esc_msg})
        old_model = current_model
        current_model = esc.escalated_model
        metrics.current_model = current_model
        metrics.escalation_count += 1
        metrics.escalation_reason = reason
        tracer.emit("model.escalated", {
            "old_model": old_model,
            "new_model": current_model,
            "reason": reason,
            "turn": turn,
            "cost_so_far": total_cost,
        })

    for turn in range(config.max_turns):
        elapsed = time.time() - start_time
        if elapsed > config.max_wall_seconds:
            tracer.emit("task.budget_exhausted", {"reason": "wall time"})
            return SubsessionResult(
                status="budget_exhausted",
                messages=messages,
                summary=last_text,
                total_turns=turn,
                total_cost_usd=total_cost,
            ), state

        try:
            check_budget(state)
        except BudgetExhausted as exc:
            tracer.emit("task.budget_exhausted", {"reason": exc.reason})
            return SubsessionResult(
                status="budget_exhausted",
                messages=messages,
                summary=last_text,
                total_turns=turn,
                total_cost_usd=total_cost,
            ), state

        # Check turn-fraction escalation before calling the model
        _maybe_escalate(turn)

        # Compress stale tool results once we hit the threshold, then every turn after
        if turn >= _COMPRESS_AFTER_TURN:
            _compress_history(messages, keep_turns=_KEEP_RECENT_TURNS)
            if turn == _COMPRESS_AFTER_TURN:
                tracer.emit("history.compressed", {"turn": turn, "keep_turns": _KEEP_RECENT_TURNS})

        api_kwargs: dict = dict(
            model=current_model,
            messages=messages,
            temperature=0,
            **_token_limit_kwargs(current_model, config.max_tokens),
        )
        if config.tools_schema:
            api_kwargs["tools"] = config.tools_schema

        response = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                response = client.chat.completions.create(**api_kwargs)
                break
            except openai.RateLimitError as exc:
                wait = min(2 ** attempt * 5, 60)
                tracer.emit("model_error", {"error": str(exc), "turn": turn, "retry_in": wait})
                if attempt == max_attempts - 1:
                    return SubsessionResult(
                        status="error",
                        messages=messages,
                        summary=f"Rate limit exhausted after retries: {exc}",
                        total_turns=turn,
                        total_cost_usd=total_cost,
                    ), state
                time.sleep(wait)
            except openai.APIError as exc:
                tracer.emit("model_error", {"error": str(exc), "turn": turn})
                return SubsessionResult(
                    status="error",
                    messages=messages,
                    summary=f"API error: {exc}",
                    total_turns=turn,
                    total_cost_usd=total_cost,
                ), state
        assert response is not None

        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = estimate_cost(current_model, input_tokens, output_tokens)
        total_cost += cost
        state = state.apply_update({
            "budgets": state.budgets.copy_with(
                used_llm_turns=state.budgets.used_llm_turns + 1,
                used_cost_usd=state.budgets.used_cost_usd + cost,
            )
        })

        tracer.emit("model_response", {
            "turn": turn,
            "model": current_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "stop_reason": finish_reason,
        })

        last_text = msg.content or last_text

        # Serialize assistant message into conversation history
        assistant_msg: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        if finish_reason == "tool_calls" and msg.tool_calls:
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result_str = registry.execute(tc.function.name, args, state.workspace_path)
                if metrics is not None:
                    record_tool_result(metrics, tc.function.name, args, result_str)
                state = state.apply_update({
                    "budgets": state.budgets.copy_with(
                        used_tool_calls=state.budgets.used_tool_calls + 1,
                    )
                })
                tracer.emit("tool_call", {
                    "name": tc.function.name,
                    "result_preview": result_str[:200],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })
            # Check escalation after all tool calls in this turn
            _maybe_escalate(turn)
        else:
            done: IsDoneOutput = check_is_done(
                task_text=state.task_text,
                last_assistant_message=last_text,
                state=state,
            )
            tracer.emit("is_done_check", {
                "is_done": done.is_done,
                "confidence": done.confidence,
                "reason": done.reason,
                "missing_steps": done.missing_steps,
            })

            if done.is_done:
                return SubsessionResult(
                    status="done",
                    messages=messages,
                    summary=last_text,
                    total_turns=turn + 1,
                    total_cost_usd=total_cost,
                ), state

            if metrics is not None:
                metrics.failed_completion_checks += 1

            nudge = build_nudge(done.missing_steps, done.reason)
            messages.append({"role": "user", "content": nudge})

            # Check escalation after failed completion check (also detects blocked text)
            _maybe_escalate(turn, last_text)

    return SubsessionResult(
        status="budget_exhausted",
        messages=messages,
        summary=last_text,
        total_turns=config.max_turns,
        total_cost_usd=total_cost,
    ), state

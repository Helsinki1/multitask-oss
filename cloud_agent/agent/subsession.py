"""LLM subsession: the core agent loop (tool-call cycle + done-checking)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import openai

from cloud_agent.agent.budgets import BudgetExhausted, check_budget, estimate_cost
from cloud_agent.agent.completion_checker import IsDoneOutput, check_is_done
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


@dataclass
class SubsessionResult:
    status: str  # "done" | "budget_exhausted" | "error"
    messages: list[dict] = field(default_factory=list)
    summary: str = ""
    total_turns: int = 0
    total_cost_usd: float = 0.0


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

        api_kwargs: dict = dict(
            model=config.model,
            max_tokens=config.max_tokens,
            messages=messages,
        )
        if config.tools_schema:
            api_kwargs["tools"] = config.tools_schema

        try:
            response = client.chat.completions.create(**api_kwargs)
        except openai.APIError as exc:
            tracer.emit("model_error", {"error": str(exc), "turn": turn})
            return SubsessionResult(
                status="error",
                messages=messages,
                summary=f"API error: {exc}",
                total_turns=turn,
                total_cost_usd=total_cost,
            ), state

        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost = estimate_cost(config.model, input_tokens, output_tokens)
        total_cost += cost
        state = state.apply_update({
            "budgets": state.budgets.copy_with(
                used_llm_turns=state.budgets.used_llm_turns + 1,
                used_cost_usd=state.budgets.used_cost_usd + cost,
            )
        })

        tracer.emit("model_response", {
            "turn": turn,
            "model": config.model,
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

            nudge = build_nudge(done.missing_steps, done.reason)
            messages.append({"role": "user", "content": nudge})

    return SubsessionResult(
        status="budget_exhausted",
        messages=messages,
        summary=last_text,
        total_turns=config.max_turns,
        total_cost_usd=total_cost,
    ), state

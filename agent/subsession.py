"""LLM subsession: the agent tool-call loop.

Runs until the model produces a text response (no tool calls) or budget is exhausted.
Verification of correctness is handled deterministically by the VERIFY node — this
loop has no completion checker, no self-assessment, no nudging.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import openai

from agent.budgets import BudgetExhausted, check_budget, estimate_cost
from agent.state import AgentState
from cloud_agent.config import settings
from observability.tracer import Tracer
from tools.registry import ToolRegistry


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


_COMPRESS_AFTER_TURN = 20
_KEEP_RECENT_TURNS = 6


def _token_limit_kwargs(model: str, max_tokens: int) -> dict[str, int]:
    if model.startswith("gpt-5") or model.startswith("codex-"):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _compress_history(messages: list[dict], keep_turns: int = _KEEP_RECENT_TURNS) -> None:
    """Truncate tool message content beyond keep_turns boundaries. Mutates in place."""
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

    for turn in range(config.max_turns):
        if time.time() - start_time > config.max_wall_seconds:
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

        if turn >= _COMPRESS_AFTER_TURN:
            _compress_history(messages)
            if turn == _COMPRESS_AFTER_TURN:
                tracer.emit("history.compressed", {"turn": turn, "keep_turns": _KEEP_RECENT_TURNS})

        api_kwargs: dict = dict(
            model=config.model,
            messages=messages,
            temperature=0,
            **_token_limit_kwargs(config.model, config.max_tokens),
        )
        if config.tools_schema:
            api_kwargs["tools"] = config.tools_schema
            api_kwargs["parallel_tool_calls"] = False

        response = None
        for attempt in range(3):
            try:
                response = client.chat.completions.create(**api_kwargs)
                break
            except openai.RateLimitError as exc:
                wait = min(2 ** attempt * 5, 60)
                tracer.emit("model_error", {"error": str(exc), "turn": turn, "retry_in": wait})
                if attempt == 2:
                    return SubsessionResult(
                        status="error",
                        messages=messages,
                        summary=f"Rate limit exhausted: {exc}",
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
            "stop_reason": choice.finish_reason,
        })

        last_text = msg.content or last_text

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

        if choice.finish_reason == "tool_calls" and msg.tool_calls:
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
            # Model produced a text response — implementation turn is complete
            return SubsessionResult(
                status="done",
                messages=messages,
                summary=last_text,
                total_turns=turn + 1,
                total_cost_usd=total_cost,
            ), state

    return SubsessionResult(
        status="budget_exhausted",
        messages=messages,
        summary=last_text,
        total_turns=config.max_turns,
        total_cost_usd=total_cost,
    ), state

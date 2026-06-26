"""Budget enforcement. Raises BudgetExhausted when limits are hit."""

from cloud_agent.agent.state import AgentState


class BudgetExhausted(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# Approximate cost per million tokens (USD)
_COST_PER_M_INPUT: dict[str, float] = {
    "gpt-4o": 75.0,
    "gpt-4o": 2.5,
    "gpt-4o-mini": 0.15,
}
_COST_PER_M_OUTPUT: dict[str, float] = {
    "gpt-4o": 150.0,
    "gpt-4o": 10.0,
    "gpt-4o-mini": 0.6,
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_rate = _COST_PER_M_INPUT.get(model, 15.0)
    out_rate = _COST_PER_M_OUTPUT.get(model, 75.0)
    return (input_tokens / 1_000_000) * inp_rate + (output_tokens / 1_000_000) * out_rate


def check_budget(state: AgentState) -> None:
    """Raise BudgetExhausted if any limit is exceeded."""
    b = state.budgets
    if b.used_llm_turns >= b.max_llm_turns:
        raise BudgetExhausted(f"LLM turn limit reached ({b.max_llm_turns})")
    if b.used_cost_usd >= b.max_cost_usd:
        raise BudgetExhausted(
            f"Cost budget exhausted (${b.used_cost_usd:.2f} >= ${b.max_cost_usd:.2f})"
        )
    if b.used_tool_calls >= b.max_tool_calls:
        raise BudgetExhausted(f"Tool call limit reached ({b.max_tool_calls})")

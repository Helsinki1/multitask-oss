from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    analyze_node,
    apply_patch_node,
    generate_patch_node,
    list_files_node,
    plan_node,
    read_files_node,
    run_tests_node,
    summarize_node,
)
from agent.state import AgentState


def _should_continue(state: AgentState) -> str:
    if state.get("test_passed") or state.get("iteration", 0) >= state.get("max_iterations", 5):
        return "summarize"
    return "generate_patch"


def build_graph() -> StateGraph:
    g = StateGraph(AgentState)

    g.add_node("list_files", list_files_node)
    g.add_node("read_files", read_files_node)
    g.add_node("plan", plan_node)
    g.add_node("generate_patch", generate_patch_node)
    g.add_node("apply_patch", apply_patch_node)
    g.add_node("run_tests", run_tests_node)
    g.add_node("analyze", analyze_node)
    g.add_node("summarize", summarize_node)

    g.add_edge(START, "list_files")
    g.add_edge("list_files", "read_files")
    g.add_edge("read_files", "plan")
    g.add_edge("plan", "generate_patch")
    g.add_edge("generate_patch", "apply_patch")
    g.add_edge("apply_patch", "run_tests")
    g.add_edge("run_tests", "analyze")
    g.add_conditional_edges("analyze", _should_continue, {"generate_patch": "generate_patch", "summarize": "summarize"})
    g.add_edge("summarize", END)

    return g.compile()

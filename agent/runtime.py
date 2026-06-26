"""BlueprintEngine: orchestrates nodes in sequence, persists state after each."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.state import AgentState, NodeError
from db.store import StateStore
from observability.tracer import Tracer


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NodeResult:
    next_node: str
    state_update: dict = field(default_factory=dict)
    status: str = "ok"  # "ok" | "warning" | "failed" | "end"


class Node:
    name: str = "unknown"
    node_type: str = "deterministic"
    failure_next: str = "END"

    def run(self, state: AgentState) -> NodeResult:
        raise NotImplementedError


class BlueprintEngine:
    def __init__(
        self,
        nodes: dict[str, Node],
        state_store: StateStore,
        tracer: Tracer,
    ) -> None:
        self.nodes = nodes
        self.state_store = state_store
        self.tracer = tracer

    def run(self, state: AgentState) -> AgentState:
        while state.current_node != "END":
            node_name = state.current_node
            node = self.nodes.get(node_name)

            if node is None:
                self.tracer.emit("node.error", {
                    "node": node_name,
                    "error": f"Node '{node_name}' not found in registry",
                })
                state = state.with_node("END").with_status("failed")
                self.state_store.save(state)
                break

            started_at = _now_iso()
            self.tracer.emit("node.start", {"node": node_name})
            self.state_store.save(state)  # persist before running

            try:
                result = node.run(state)
                ended_at = _now_iso()

                state = state.apply_update(result.state_update).with_node(result.next_node)

                self.tracer.emit("node.complete", {
                    "node": node_name,
                    "next": result.next_node,
                    "status": result.status,
                })
                self.state_store.record_node_run(
                    state.session_id, node_name, node.node_type,
                    started_at, ended_at, result.status,
                )

            except Exception as exc:
                ended_at = _now_iso()
                error_msg = f"{type(exc).__name__}: {exc}"
                self.tracer.emit("node.error", {"node": node_name, "error": error_msg})

                node_err = NodeError(
                    node=node_name,
                    message=str(exc),
                    exception_type=type(exc).__name__,
                    retryable=False,
                )
                failure_next = getattr(node, "failure_next", "END")
                state = state.add_error(node_err).with_node(failure_next)

                self.state_store.record_node_run(
                    state.session_id, node_name, node.node_type,
                    started_at, ended_at, "failed", error_msg,
                )

            self.state_store.save(state)  # persist after running

        # Mark final state
        if state.task_status == "running":
            state = state.with_status("done")
        self.state_store.finalize_session(state.session_id, state.task_status)
        self.state_store.save(state)
        return state

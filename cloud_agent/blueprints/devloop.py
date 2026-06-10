"""Devloop blueprint: assembles the node graph for Phase 1 (nodes 01-05)."""

from cloud_agent.agent.runtime import BlueprintEngine, Node, NodeResult
from cloud_agent.agent.state import AgentState
from cloud_agent.blueprints.nodes.check_branch import CheckBranchNode
from cloud_agent.blueprints.nodes.checkpoint import CheckpointNode
from cloud_agent.blueprints.nodes.implement_task import ImplementTaskNode
from cloud_agent.blueprints.nodes.load_task import LoadTaskNode
from cloud_agent.blueprints.nodes.prepare_context import PrepareContextNode
from cloud_agent.db.store import StateStore
from cloud_agent.observability.tracer import Tracer


class EndNode(Node):
    name = "END"
    node_type = "terminal"

    def run(self, state: AgentState) -> NodeResult:
        return NodeResult(next_node="END", status="end")


def build_devloop(tracer: Tracer, state_store: StateStore) -> BlueprintEngine:
    nodes: dict[str, Node] = {
        "01_CHECK_BRANCH": CheckBranchNode(),
        "02_LOAD_TASK": LoadTaskNode(),
        "03_PREPARE_CONTEXT": PrepareContextNode(),
        "04_IMPLEMENT_TASK": ImplementTaskNode(tracer=tracer),
        "05_CHECKPOINT": CheckpointNode(),
        "END": EndNode(),
    }
    return BlueprintEngine(nodes=nodes, state_store=state_store, tracer=tracer)

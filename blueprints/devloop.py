"""Devloop blueprint: assembles the node graph for Phase 1 (nodes 01-05)."""

from agent.runtime import BlueprintEngine, Node, NodeResult
from agent.state import AgentState
from blueprints.nodes.check_branch import CheckBranchNode
from blueprints.nodes.checkpoint import CheckpointNode
from blueprints.nodes.implement_task import ImplementTaskNode
from blueprints.nodes.load_task import LoadTaskNode
from blueprints.nodes.prepare_context import PrepareContextNode
from blueprints.nodes.reproduce_issue import ReproduceIssueNode
from blueprints.nodes.seed_scripts import SeedScriptsNode
from blueprints.nodes.verify_fix import VerifyFixNode
from db.store import StateStore
from observability.tracer import Tracer


class EndNode(Node):
    name = "END"
    node_type = "terminal"

    def run(self, state: AgentState) -> NodeResult:
        return NodeResult(next_node="END", status="end")


def build_devloop(tracer: Tracer, state_store: StateStore) -> BlueprintEngine:
    nodes: dict[str, Node] = {
        "01_CHECK_BRANCH": CheckBranchNode(),
        "SEED_SCRIPTS": SeedScriptsNode(),
        "02_LOAD_TASK": LoadTaskNode(),
        "03_PREPARE_CONTEXT": PrepareContextNode(),
        "REPRODUCE_ISSUE": ReproduceIssueNode(tracer=tracer),
        "04_IMPLEMENT_TASK": ImplementTaskNode(tracer=tracer),
        "VERIFY_FIX": VerifyFixNode(tracer=tracer),
        "05_CHECKPOINT": CheckpointNode(),
        "END": EndNode(),
    }
    return BlueprintEngine(nodes=nodes, state_store=state_store, tracer=tracer)

"""Devloop blueprint: assembles the node graph.

Bug fix path:
  CHECK_BRANCH → LOAD_TASK → GATHER_CONTEXT → IMPLEMENT → VERIFY
                                    ↑ (p2p regression)        |
                                    └──────────────────────────┤
                              IMPLEMENT ←── (f2p still failing)┤
                                                    CHECKPOINT ←┘ (all pass or max retries)

Additive path:
  CHECK_BRANCH → LOAD_TASK → GATHER_ADDITIVE_CONTEXT → DEFINE_CONTRACT → IMPLEMENT
                                                                ↑              |
                                                           (still failing)     ↓
                                                              VERIFY_ADDITIVE → CHECKPOINT
"""

from agent.runtime import BlueprintEngine, Node, NodeResult
from agent.state import AgentState
from blueprints.nodes.check_branch import CheckBranchNode
from blueprints.nodes.checkpoint import CheckpointNode
from blueprints.nodes.define_contract import DefineContractNode
from blueprints.nodes.gather_additive_context import GatherAdditiveContextNode
from blueprints.nodes.gather_context import GatherContextNode
from blueprints.nodes.implement import ImplementNode
from blueprints.nodes.load_task import LoadTaskNode
from blueprints.nodes.verify import VerifyNode
from blueprints.nodes.verify_additive import VerifyAdditiveNode
from db.store import StateStore
from observability.tracer import Tracer


class EndNode(Node):
    name = "END"
    node_type = "terminal"

    def run(self, state: AgentState) -> NodeResult:
        return NodeResult(next_node="END", status="end")


def build_devloop(tracer: Tracer, state_store: StateStore) -> BlueprintEngine:
    nodes: dict[str, Node] = {
        "CHECK_BRANCH": CheckBranchNode(),
        "LOAD_TASK": LoadTaskNode(),
        # Bug fix path
        "GATHER_CONTEXT": GatherContextNode(tracer=tracer),
        "IMPLEMENT": ImplementNode(tracer=tracer),
        "VERIFY": VerifyNode(tracer=tracer),
        # Additive path
        "GATHER_ADDITIVE_CONTEXT": GatherAdditiveContextNode(tracer=tracer),
        "DEFINE_CONTRACT": DefineContractNode(tracer=tracer),
        "VERIFY_ADDITIVE": VerifyAdditiveNode(tracer=tracer),
        # Shared terminal nodes
        "CHECKPOINT": CheckpointNode(),
        "END": EndNode(),
    }
    return BlueprintEngine(nodes=nodes, state_store=state_store, tracer=tracer)

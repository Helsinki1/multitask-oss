"""Node CHECKPOINT: commit implementation changes at the end of a run."""

import subprocess

from agent.runtime import Node, NodeResult
from agent.state import AgentState


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    r = subprocess.run(["git"] + args, capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


class CheckpointNode(Node):
    name = "CHECKPOINT"
    node_type = "deterministic"
    failure_next = "END"

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path

        # Check if there's anything to commit
        rc, porcelain, _ = _git(["status", "--porcelain"], ws)
        if not porcelain.strip():
            # Nothing to commit — still succeeds
            return NodeResult(
                next_node="END",
                state_update={},
                status="ok",
            )

        # Stage all changes in the working tree
        _git(["add", "-A"], ws)

        # Commit
        msg = f"agent: implement task '{state.task_text[:60]}'"
        rc2, out2, err2 = _git(["commit", "-m", msg], ws)
        if rc2 != 0:
            raise RuntimeError(f"git commit failed: {err2}")

        # Get new HEAD SHA
        _, new_sha, _ = _git(["rev-parse", "HEAD"], ws)

        return NodeResult(
            next_node="END",
            state_update={"initial_commit_sha": new_sha},
            status="ok",
        )

"""Node CHECK_BRANCH: verify workspace safety, capture initial SHA, ensure working branch.

Verifies workspace safety, captures initial SHA, ensures we're on a working branch.
"""

import re
import subprocess

from agent.runtime import Node, NodeResult
from agent.state import AgentState
from cloud_agent.config import settings


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    r = subprocess.run(["git"] + args, capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _slugify(text: str, max_words: int = 5) -> str:
    words = re.sub(r"[^a-zA-Z0-9\s]", "", text.lower()).split()[:max_words]
    return "-".join(words) or "task"


class CheckBranchNode(Node):
    name = "CHECK_BRANCH"
    node_type = "deterministic"
    failure_next = "END"

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path

        # Check git is available
        rc, sha, err = _git(["rev-parse", "HEAD"], ws)
        if rc != 0:
            raise RuntimeError(f"git not available or not a repo: {err}")

        # Current branch
        _, branch, _ = _git(["branch", "--show-current"], ws)

        # Check for dirty workspace
        _, porcelain, _ = _git(["status", "--porcelain", "--untracked-files=no"], ws)
        if porcelain.strip():
            raise RuntimeError(
                f"Workspace has uncommitted changes. Please commit or stash before running the agent.\n{porcelain}"
            )

        # If on a protected branch, create a new working branch
        working_branch = branch
        if branch in settings.protected_branches or not branch:
            slug = _slugify(state.task_text)
            working_branch = f"agent/{state.task_id}-{slug}"
            rc2, _, err2 = _git(["checkout", "-b", working_branch], ws)
            if rc2 != 0:
                raise RuntimeError(f"Failed to create branch '{working_branch}': {err2}")

        return NodeResult(
            next_node="LOAD_TASK",
            state_update={
                "initial_commit_sha": sha,
                "working_branch": working_branch,
                "base_branch": branch or state.base_branch,
            },
        )

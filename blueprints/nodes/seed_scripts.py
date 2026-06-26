"""Node SEED_SCRIPTS: drops _agent_scripts/ into the workspace before the agent starts.

Writes docker_run.py (Docker shell helper) so the agent can spin up an isolated
container via run_shell without needing to manage container IDs manually.
Also ensures _agent_scripts/ is in .gitignore so it never leaks into the patch.
"""

import os
import pathlib

from agent.runtime import Node, NodeResult
from agent.state import AgentState

_DOCKER_RUN_PY = '''\
#!/usr/bin/env python3
"""
Purpose: Run commands inside an isolated Docker container scoped to this agent run.
Problem: Running test suites and installs directly on the host risks dependency
         conflicts and pollutes the environment; docker exec keeps side effects contained.
Usage:
  python _agent_scripts/docker_run.py "pip install -e ."
  python _agent_scripts/docker_run.py "python _repro_test.py"
  python _agent_scripts/docker_run.py "python -m pytest tests/ -x -q 2>&1 | tail -40"
  python _agent_scripts/docker_run.py --status

Container is started on first call and reused for the session.
The workspace is bind-mounted at /workspace (read-write).
"""
import os
import subprocess
import sys
import pathlib

_STATE = pathlib.Path("/tmp/_agent_docker_cid")
IMAGE = os.environ.get("AGENT_DOCKER_IMAGE", "python:3.10-slim")
WORKSPACE = str(pathlib.Path(os.environ.get("AGENT_WORKSPACE", os.getcwd())).resolve())


def _running(cid: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", cid],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def _start() -> str:
    r = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "-v", f"{WORKSPACE}:/workspace",
            "-w", "/workspace",
            "-e", "PAGER=cat",
            "-e", "TQDM_DISABLE=1",
            "-e", "PIP_PROGRESS_BAR=off",
            "--network=none",
            IMAGE, "tail", "-f", "/dev/null",
        ],
        capture_output=True, text=True, check=True,
    )
    cid = r.stdout.strip()
    _STATE.write_text(cid)
    return cid


def _get_container() -> str:
    if _STATE.exists():
        cid = _STATE.read_text().strip()
        if _running(cid):
            return cid
    return _start()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--status":
        cid = _get_container()
        print(f"container: {cid}  image: {IMAGE}  workspace: {WORKSPACE}")
        sys.exit(0)
    cid = _get_container()
    r = subprocess.run(
        ["docker", "exec", "-w", "/workspace", cid, "bash", "-c", " ".join(args)],
        text=True,
    )
    sys.exit(r.returncode)
'''


class SeedScriptsNode(Node):
    name = "SEED_SCRIPTS"
    node_type = "deterministic"
    failure_next = "END"

    def run(self, state: AgentState) -> NodeResult:
        scripts_dir = pathlib.Path(state.workspace_path) / "_agent_scripts"
        scripts_dir.mkdir(exist_ok=True)

        docker_run = scripts_dir / "docker_run.py"
        docker_run.write_text(_DOCKER_RUN_PY)
        docker_run.chmod(0o755)

        self._ensure_gitignore(state.workspace_path)

        return NodeResult(next_node="02_LOAD_TASK")

    @staticmethod
    def _ensure_gitignore(workspace: str) -> None:
        gitignore = pathlib.Path(workspace) / ".gitignore"
        entry = "_agent_scripts/"
        if gitignore.exists():
            if entry in gitignore.read_text().splitlines():
                return
            with gitignore.open("a") as f:
                f.write(f"\n{entry}\n")
        else:
            gitignore.write_text(f"{entry}\n")

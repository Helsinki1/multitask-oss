"""Node SEED_SCRIPTS: drops diagnostic helpers into _agent_scripts/ before the agent starts.

Three scripts are seeded:
  mro_check.py    — full Python MRO dump with __slots__ at every level (inheritance bugs)
  import_graph.py — imports in/out of any file (dependency tracing)
  docker_run.py   — isolated Docker shell (environment isolation)

These are described in LAYER_2_ENV so the agent knows they exist on turn 1.
_agent_scripts/ is added to .gitignore so helpers never leak into the final patch.
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


_MRO_CHECK_PY = '''\
#!/usr/bin/env python3
"""
Purpose: Dump the full Python MRO of a class, showing __slots__ at every level.
Problem: __slots__/__dict__ bugs require seeing the entire inheritance chain at once.
         Reading files one by one misses the specific ancestor that causes __dict__ leakage.
Usage:
  python _agent_scripts/mro_check.py sympy.core.symbol.Symbol
  python _agent_scripts/mro_check.py django.db.models.Model
"""
import sys, importlib

def main():
    if len(sys.argv) < 2:
        print("Usage: mro_check.py <module.path.ClassName>"); sys.exit(1)
    dotted = sys.argv[1]
    mod_path, cls_name = dotted.rsplit(".", 1)
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        print(f"ERROR importing {mod_path!r}: {e}"); sys.exit(1)
    cls = getattr(mod, cls_name, None)
    if cls is None:
        print(f"ERROR: {cls_name!r} not found in {mod_path!r}"); sys.exit(1)
    print(f"MRO for {dotted}:\\n")
    print(f"  {'Class':<45} {'Module':<35} __slots__")
    print("  " + "-" * 100)
    for c in cls.__mro__:
        slots = c.__dict__.get("__slots__", "*** MISSING — instance will have __dict__ ***")
        print(f"  {c.__name__:<45} {getattr(c, '__module__', '?'):<35} {slots!r}")

if __name__ == "__main__":
    main()
'''

_IMPORT_GRAPH_PY = '''\
#!/usr/bin/env python3
"""
Purpose: Show what a file imports, and which workspace files import it.
Problem: Dependency tracing requires checking both directions; manual grep loses the picture.
Usage:
  python _agent_scripts/import_graph.py sympy/core/symbol.py
  python _agent_scripts/import_graph.py django/db/models/base.py
"""
import ast, os, subprocess, sys

def extract_imports(fp):
    try:
        tree = ast.parse(open(fp, encoding="utf-8", errors="ignore").read())
    except (SyntaxError, OSError) as e:
        return [f"(parse error: {e})"]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(f"from {node.module} import {', '.join(a.name for a in node.names)}")
        elif isinstance(node, ast.Import):
            out.append(f"import {', '.join(a.name for a in node.names)}")
    return out

def find_dependents(fp, ws):
    rel = os.path.relpath(fp, ws)
    patterns = [rel.replace(os.sep, ".").removesuffix(".py"), rel.removesuffix(".py"),
                os.path.splitext(os.path.basename(fp))[0]]
    found = set()
    for pat in patterns:
        r = subprocess.run(["grep", "-rln", "--include=*.py", pat, ws],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and os.path.realpath(line) != os.path.realpath(fp):
                found.add(os.path.relpath(line, ws))
    return sorted(found)[:20]

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: import_graph.py <path/to/file.py>"); sys.exit(1)
    fp = sys.argv[1] if os.path.isabs(sys.argv[1]) else os.path.join(os.getcwd(), sys.argv[1])
    if not os.path.isfile(fp):
        print(f"ERROR: {sys.argv[1]} not found"); sys.exit(1)
    ws = os.getcwd()
    rel = os.path.relpath(fp, ws)
    print(f"=== Imports declared IN {rel} ===")
    for imp in extract_imports(fp): print(f"  {imp}")
    print(f"\\n=== Workspace files that import {rel} ===")
    deps = find_dependents(fp, ws)
    for d in (deps or ["(none found)"]): print(f"  {d}")
'''


class SeedScriptsNode(Node):
    name = "SEED_SCRIPTS"
    node_type = "deterministic"
    failure_next = "END"

    def run(self, state: AgentState) -> NodeResult:
        scripts_dir = pathlib.Path(state.workspace_path) / "_agent_scripts"
        scripts_dir.mkdir(exist_ok=True)

        for name, content in [
            ("mro_check.py", _MRO_CHECK_PY),
            ("import_graph.py", _IMPORT_GRAPH_PY),
            ("docker_run.py", _DOCKER_RUN_PY),
        ]:
            p = scripts_dir / name
            p.write_text(content)
            p.chmod(0o755)

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

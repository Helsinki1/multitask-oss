"""PREPARE_CONTEXT logic: build a ContextBundle from the repo."""

import os

from cloud_agent.agent.state import ContextBundle


_RULE_FILES = ["AGENTS.md", "CLAUDE.md", ".cursor/rules", ".github/copilot-instructions.md"]


def build_context_bundle(workspace: str, task_text: str) -> ContextBundle:
    cb = ContextBundle(task_summary=task_text)

    # 1. Repository rules from AGENTS.md / CLAUDE.md etc.
    for fname in _RULE_FILES:
        path = os.path.join(workspace, fname)
        if os.path.exists(path):
            try:
                content = open(path, encoding="utf-8").read(8000)
                cb.repo_rules.append(f"[from {fname}]\n{content}")
            except OSError:
                pass

    # 2. Build/test commands from manifest files
    cb.build_and_test_commands = _detect_build_commands(workspace)

    # 3. Lightweight repo map
    cb.repo_map = _generate_repo_map(workspace)

    return cb


def _detect_build_commands(workspace: str) -> list[str]:
    cmds: list[str] = []

    # pyproject.toml / setup.cfg -> pytest
    if os.path.exists(os.path.join(workspace, "pyproject.toml")):
        try:
            content = open(os.path.join(workspace, "pyproject.toml")).read()
            if "pytest" in content:
                cmds.append("pytest")
            else:
                cmds.append("python -m pytest")
        except OSError:
            cmds.append("python -m pytest")

    # Makefile
    mf = os.path.join(workspace, "Makefile")
    if os.path.exists(mf):
        try:
            for line in open(mf).readlines()[:60]:
                if line.startswith(("test", "check")):
                    target = line.split(":")[0].strip()
                    cmds.append(f"make {target}")
                    break
        except OSError:
            pass

    # package.json
    pj = os.path.join(workspace, "package.json")
    if os.path.exists(pj):
        try:
            import json
            data = json.loads(open(pj).read())
            scripts = data.get("scripts", {})
            if "test" in scripts:
                cmds.append(f"npm test  # runs: {scripts['test']}")
        except Exception:
            cmds.append("npm test")

    # go.mod
    if os.path.exists(os.path.join(workspace, "go.mod")):
        cmds.append("go test ./...")

    # Default: try pytest for any .py files
    if not cmds:
        py_files = [f for f in os.listdir(workspace) if f.endswith(".py")]
        if py_files:
            cmds.append("python -m pytest")

    return cmds


def _generate_repo_map(workspace: str) -> str:
    from cloud_agent.tools.search import get_repo_map_tool
    try:
        return get_repo_map_tool({}, workspace)
    except Exception:
        return "(repo map unavailable)"

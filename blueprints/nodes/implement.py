"""Node IMPLEMENT: LLM subsession that makes the code changes.

Receives structured context (TestToDoList + pre-loaded files) from GATHER_CONTEXT
or GATHER_ADDITIVE_CONTEXT, runs the implement subsession, then routes to VERIFY.

Does NOT assess its own completion — that is handled deterministically by VERIFY.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent.prompts import (
    build_additive_system,
    build_bugfix_system,
    build_implement_human,
)
from agent.runtime import Node, NodeResult
from agent.state import AgentState, ContextBundle
from agent.subsession import SubsessionConfig, run_subsession
from cloud_agent.config import settings
from observability.tracer import Tracer
from tools.registry import ToolRegistry, build_dev_toolset


_MRO_CHECK = '''\
"""
Purpose: Dump Python MRO + __slots__ at every inheritance level for a class.
Problem: __slots__ suppression requires EVERY ancestor to declare it — a single
         class missing __slots__ = () causes the whole chain to have __dict__.
Usage:   python _agent_scripts/mro_check.py sympy.core.symbol.Symbol
         python _agent_scripts/mro_check.py sympy/core/symbol.py Symbol
"""
import importlib
import sys

def main():
    if len(sys.argv) < 2:
        print("Usage: mro_check.py dotted.module.ClassName")
        print("       mro_check.py path/to/file.py ClassName")
        sys.exit(1)

    arg = sys.argv[1]
    if arg.endswith(".py") or "/" in arg:
        if len(sys.argv) < 3:
            print("Error: provide a class name after the file path")
            print("Usage: mro_check.py path/to/file.py ClassName")
            sys.exit(1)
        module_path = arg.removesuffix(".py").replace("/", ".")
        cls_name = sys.argv[2]
    else:
        module_path, _, cls_name = arg.rpartition(".")

    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        print(f"Error: cannot import {module_path!r}: {exc}")
        sys.exit(1)
    cls = getattr(mod, cls_name)
    print(f"MRO for {module_path}.{cls_name}:")
    for c in cls.__mro__:
        slots = getattr(c, "__slots__", "** MISSING **")
        print(f"  {c.__module__}.{c.__name__}  __slots__ = {slots!r}")

if __name__ == "__main__":
    main()
'''

_IMPORT_GRAPH = '''\
"""
Purpose: Show what a file imports and what other workspace files import it.
Problem: Need to understand transitive dependencies without running the code.
Usage:   python _agent_scripts/import_graph.py sympy/core/_print_helpers.py
"""
import ast
import os
import sys

def get_imports(filepath):
    try:
        tree = ast.parse(open(filepath, errors="ignore").read())
    except Exception:
        return []
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
    return names

def main():
    if len(sys.argv) < 2:
        print("Usage: import_graph.py path/to/file.py")
        sys.exit(1)
    target = os.path.abspath(sys.argv[1])
    workspace = os.getcwd()

    print(f"Imports declared in {sys.argv[1]}:")
    for name in get_imports(target):
        print(f"  {name}")

    print(f"\\nWorkspace files that import {os.path.relpath(target, workspace)}:")
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", ".venv"}]
        for f in files:
            if not f.endswith(".py"):
                continue
            fp = os.path.join(root, f)
            if fp == target:
                continue
            src = open(fp, errors="ignore").read()
            rel_target = os.path.relpath(target, workspace).replace(os.sep, "/").replace("/", ".").removesuffix(".py")
            if rel_target.split(".")[-1] in src or os.path.basename(target).removesuffix(".py") in src:
                print(f"  {os.path.relpath(fp, workspace)}")

if __name__ == "__main__":
    main()
'''


def _seed_agent_scripts(workspace: str) -> None:
    scripts_dir = Path(workspace) / "_agent_scripts"
    scripts_dir.mkdir(exist_ok=True)

    gitignore = Path(workspace) / ".gitignore"
    try:
        existing = gitignore.read_text() if gitignore.exists() else ""
        if "_agent_scripts/" not in existing:
            with gitignore.open("a") as f:
                f.write("\n_agent_scripts/\n")
    except OSError:
        pass

    for name, content in [("mro_check.py", _MRO_CHECK), ("import_graph.py", _IMPORT_GRAPH)]:
        p = scripts_dir / name
        p.write_text(content)  # always overwrite so fixes to seeded scripts take effect


def _collect_agent_scripts(workspace: str) -> list[str]:
    """Return name+Purpose for user-created scripts in _agent_scripts/ (not seeded ones)."""
    seeded = {"mro_check.py", "import_graph.py"}
    scripts_dir = Path(workspace) / "_agent_scripts"
    if not scripts_dir.exists():
        return []
    result: list[str] = []
    for p in sorted(scripts_dir.glob("*.py")):
        if p.name in seeded:
            continue
        summary = ""
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines()[:20]:
                stripped = line.strip().strip('"""').strip("'''")
                if stripped.startswith("Purpose:"):
                    summary = stripped[len("Purpose:"):].strip()
                    break
        except OSError:
            pass
        result.append(p.name + (f" — {summary}" if summary else ""))
    return result


def _load_agent_notes(workspace: str) -> str:
    """Read notes.md written by the agent in prior attempts, if it exists."""
    notes_path = Path(workspace) / "_agent_scripts" / "notes.md"
    try:
        if notes_path.exists():
            return notes_path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        pass
    return ""


def _extract_agent_read_files(
    messages: list[dict],
    workspace: str,
    existing_paths: set[str],
) -> list[dict]:
    """Parse subsession messages for read_file calls; return files not already in context."""
    seen: set[str] = set()
    new_files: list[dict] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("function", {}).get("name") != "read_file":
                continue
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                continue
            path = args.get("path", "")
            if not path or path in seen or path in existing_paths:
                continue
            seen.add(path)
            abs_path = Path(workspace) / path
            try:
                lines = abs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if not lines:
                    continue
                content = "\n".join(lines[:200])
                if len(lines) > 200:
                    content += f"\n... ({len(lines) - 200} more lines)"
                new_files.append({
                    "path": path,
                    "content": content,
                    "why": "read by agent in prior attempt",
                })
            except OSError:
                pass
    return new_files


class ImplementNode(Node):
    name = "IMPLEMENT"
    node_type = "llm_subsession"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path
        _seed_agent_scripts(ws)

        existing_scripts = _collect_agent_scripts(ws)
        if existing_scripts:
            scripts_note = "\nUser-created scripts from prior attempts (reuse before recreating):\n" + \
                           "\n".join(f"  _agent_scripts/{s}" for s in existing_scripts)
        else:
            scripts_note = ""

        if state.task_type == "additive":
            system = build_additive_system(state)
        else:
            system = build_bugfix_system(state)

        if scripts_note:
            system = system + "\n\n---\n\n" + scripts_note.strip()

        prior_notes = _load_agent_notes(ws)
        if prior_notes:
            system = system + "\n\n---\n\nNotes from your prior attempt(s):\n" + prior_notes

        registry = ToolRegistry()
        build_dev_toolset(registry)

        config = SubsessionConfig(
            name="implement",
            system_prompt=system,
            initial_human_message=build_implement_human(state),
            model=settings.implement_model,
            tools_schema=registry.to_openai_schema(),
            max_turns=state.budgets.max_llm_turns,
            max_wall_seconds=state.budgets.max_wall_seconds,
            max_tokens=8192,
        )

        result, updated_state = run_subsession(config, state, registry, self.tracer)

        self.tracer.emit("subsession.done", {
            "status": result.status,
            "turns": result.total_turns,
            "cost_usd": result.total_cost_usd,
        })

        # If budget was already exhausted before this subsession did any work, skip VERIFY
        # and go straight to CHECKPOINT — avoids burning MAX_VERIFY_ATTEMPTS on empty loops.
        if result.status == "budget_exhausted" and result.total_turns == 0:
            self.tracer.emit("implement.budget_exhausted_noop", {})
            return NodeResult(
                next_node="CHECKPOINT",
                state_update={"budgets": updated_state.budgets},
                status="warning",
            )

        # Carry forward files the agent read this attempt into the context bundle
        existing_paths = {f["path"] for f in state.context_bundle.task_adjacent_files}
        new_files = _extract_agent_read_files(result.messages, ws, existing_paths)
        if new_files:
            self.tracer.emit("implement.context_carried_forward", {
                "files": [f["path"] for f in new_files],
            })
        updated_bundle = ContextBundle(
            repo_rules=state.context_bundle.repo_rules,
            build_and_test_commands=state.context_bundle.build_and_test_commands,
            task_adjacent_files=state.context_bundle.task_adjacent_files + new_files,
        )

        next_node = "VERIFY" if state.task_type == "bug_fix" else "VERIFY_ADDITIVE"

        return NodeResult(
            next_node=next_node,
            state_update={
                "budgets": updated_state.budgets,
                "context_bundle": updated_bundle,
            },
            status="ok" if result.status == "done" else "warning",
        )

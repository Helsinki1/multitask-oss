"""Tool registry: maps tool names to callables and Anthropic schema definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict, str], str]
    timeout_seconds: int = 120


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def to_anthropic_schema(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: dict, workspace: str) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return f"Error: unknown tool '{name}'"
        try:
            return spec.fn(args, workspace)
        except Exception as exc:
            return f"Error in {name}: {exc}"


def build_dev_toolset(registry: ToolRegistry) -> None:
    """Register all Phase-1 development tools into registry."""
    from cloud_agent.tools.filesystem import (
        apply_patch_tool,
        read_file_tool,
        replace_in_file_tool,
        write_file_tool,
    )
    from cloud_agent.tools.git import git_diff_tool, git_log_tool, git_status_tool
    from cloud_agent.tools.search import get_repo_map_tool, list_files_tool, search_repo_tool
    from cloud_agent.tools.shell import run_shell_tool
    from cloud_agent.tools.tests import run_tests_tool

    registry.register(ToolSpec(
        name="read_file",
        description=(
            "Read a file from the repository. Returns line-numbered content. "
            "Use start_line/end_line to read a slice of a large file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from repo root"},
                "start_line": {"type": "integer", "description": "First line to read (1-indexed)"},
                "end_line": {"type": "integer", "description": "Last line to read (inclusive)"},
            },
            "required": ["path"],
        },
        fn=read_file_tool,
    ))

    registry.register(ToolSpec(
        name="write_file",
        description="Create a new file. Do NOT use to overwrite existing files — use replace_in_file instead.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        fn=write_file_tool,
    ))

    registry.register(ToolSpec(
        name="replace_in_file",
        description=(
            "Replace exact text in an existing file. Fails if old_text not found. "
            "Prefer this over apply_patch for targeted single-location edits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Exact text to find"},
                "new_text": {"type": "string", "description": "Replacement text"},
                "expected_replacements": {
                    "type": "integer",
                    "description": "Expected number of replacements (default 1 — fails if count differs)",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
        fn=replace_in_file_tool,
    ))

    registry.register(ToolSpec(
        name="apply_patch",
        description="Apply a unified diff patch to the repository. Use for multi-file or complex edits.",
        input_schema={
            "type": "object",
            "properties": {
                "patch": {"type": "string", "description": "Unified diff patch text (--- a/... +++ b/...)"},
            },
            "required": ["patch"],
        },
        fn=apply_patch_tool,
    ))

    registry.register(ToolSpec(
        name="list_files",
        description="List files and directories in the repository.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subdirectory to list (default: repo root)"},
                "max_depth": {"type": "integer", "description": "Max depth (default: 3)"},
            },
            "required": [],
        },
        fn=list_files_tool,
    ))

    registry.register(ToolSpec(
        name="search_repo",
        description=(
            "Search for text/patterns across the repository using grep. "
            "Returns matches with file, line number, and surrounding context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search pattern (literal string or regex)"},
                "path": {"type": "string", "description": "Restrict to this subdirectory"},
                "file_glob": {"type": "string", "description": "Only search files matching glob (e.g. '*.py')"},
            },
            "required": ["query"],
        },
        fn=search_repo_tool,
    ))

    registry.register(ToolSpec(
        name="get_repo_map",
        description="Get a structural overview of the repository: directory tree, key files, classes, functions.",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        fn=get_repo_map_tool,
    ))

    registry.register(ToolSpec(
        name="run_shell",
        description=(
            "Run a shell command in the repository. Returns exit code, stdout, stderr. "
            "Use for install, build, lint, and other shell operations."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Working directory relative to repo root"},
                "timeout_seconds": {"type": "integer", "description": "Timeout (default: 120s, max: 600s)"},
            },
            "required": ["command"],
        },
        fn=run_shell_tool,
        timeout_seconds=120,
    ))

    registry.register(ToolSpec(
        name="run_tests",
        description=(
            "Run tests with parsed output (passed/failed counts, failing test names). "
            "Use for pytest, npm test, go test, etc."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Test command (e.g. 'pytest', 'npm test')"},
                "cwd": {"type": "string", "description": "Working directory relative to repo root"},
                "timeout_seconds": {"type": "integer", "description": "Timeout (default: 300s)"},
            },
            "required": ["command"],
        },
        fn=run_tests_tool,
        timeout_seconds=300,
    ))

    registry.register(ToolSpec(
        name="git_status",
        description="Get current git status: branch, staged/unstaged/untracked files.",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        fn=git_status_tool,
    ))

    registry.register(ToolSpec(
        name="git_diff",
        description="Get the git diff. Pass base to diff against a specific ref (default: HEAD).",
        input_schema={
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Base ref to diff against (default: HEAD)"},
            },
            "required": [],
        },
        fn=git_diff_tool,
    ))

    registry.register(ToolSpec(
        name="git_log",
        description="Get recent git commit history.",
        input_schema={
            "type": "object",
            "properties": {
                "max_count": {"type": "integer", "description": "Number of commits to show (default: 10)"},
            },
            "required": [],
        },
        fn=git_log_tool,
    ))

"""Tool registry: maps tool names to callables and OpenAI schema definitions."""

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

    def to_openai_schema(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
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
    """Register the minimal 4-tool set: shell + three structured file operations.

    Everything else (git, search, list, tests) is done via run_shell bash commands.
    """
    from tools.filesystem import (
        read_file_tool,
        replace_in_file_tool,
        write_file_tool,
    )
    from tools.shell import run_shell_tool

    registry.register(ToolSpec(
        name="run_shell",
        description=(
            "Run any bash command in the repository. Returns exit code, stdout, stderr. "
            "Use for exploration (find, grep, ls), git, tests, install, build, and lint. "
            "Prefer this over separate tools for git/search/test operations."
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
        name="read_file",
        description=(
            "Read a file with line numbers. Use start_line/end_line to read a slice of a large file "
            "instead of dumping the whole thing. Prefer this over cat for files over ~100 lines."
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
            "Replace exact text in an existing file. Fails if old_text is not found. "
            "Use for targeted edits: old_text must match the file exactly including whitespace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Exact text to find and replace"},
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

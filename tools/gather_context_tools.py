"""Stateful read-queue tools for the GATHER_CONTEXT subsession.

Unlike the stateless dev tools (filesystem.py, shell.py), these close over a
per-run GatherQueueState so the harness can deterministically enforce the
read-queue discipline: every dequeued file must be noted (or explicitly
flagged not relevant) before the next one is served, and the queue must
drain before the subsession is allowed to finish (enforced via
SubsessionConfig.on_finish_check in the calling node).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from tools.filesystem import _resolve
from tools.registry import ToolRegistry, ToolSpec

MAX_QUEUE_FILES = 25
_READ_LINE_CAP = 2000


@dataclass
class GatherQueueState:
    queue: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    notes: list[dict] = field(default_factory=list)
    current: str | None = None

    def seed(self, paths: list[str]) -> None:
        for p in paths:
            if p not in self.seen:
                self.seen.add(p)
                self.queue.append(p)


def _read_numbered(full_path: str, start: int | None = None, end: int | None = None) -> tuple[str, int]:
    """Read a file with line numbers. Returns (rendered_text, total_lines)."""
    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    total = len(lines)
    offset = (start - 1) if start else 0
    if start:
        end_idx = end if end else total
        sliced = lines[start - 1 : end_idx]
    else:
        sliced = lines[:_READ_LINE_CAP]
    out = "".join(f"{offset + i + 1:4d} | {line}" for i, line in enumerate(sliced))
    return out, total


def _note_has_path(qs: GatherQueueState, path: str) -> bool:
    return any(n["path"] == path for n in qs.notes)


def make_enqueue_tool(qs: GatherQueueState):
    def enqueue_tool(args: dict, workspace: str) -> str:
        path = args["path"]
        reason = args.get("reason", "")
        try:
            full = _resolve(path, workspace)
        except ValueError as e:
            return f"Error: {e}"
        if not os.path.isfile(full):
            return f"Error: file not found: {path}"
        if path in qs.seen:
            return f"'{path}' is already queued or read — skipping duplicate enqueue."
        if len(qs.seen) >= MAX_QUEUE_FILES:
            return (
                f"Error: read-queue file cap ({MAX_QUEUE_FILES}) reached — "
                "finish up with what you've already read instead of enqueuing more."
            )
        qs.seen.add(path)
        qs.queue.append(path)
        return f"Enqueued '{path}' ({reason or 'no reason given'}). Queue depth: {len(qs.queue)}."

    return enqueue_tool


def make_dequeue_tool(qs: GatherQueueState):
    def dequeue_next_tool(args: dict, workspace: str) -> str:
        if qs.current is not None and not _note_has_path(qs, qs.current):
            return (
                f"Error: you haven't called note() on '{qs.current}' yet. Call note() "
                "first — if nothing in it is relevant, note it with start_line=1, "
                "end_line=1, why=\"not relevant\" — then call dequeue_next() again."
            )
        if not qs.queue:
            qs.current = None
            return (
                "Read-queue is empty. If you're confident you have everything you need, "
                "respond with your summary now (no tool call) to finish. Otherwise call "
                "enqueue(path, reason) for anything else worth investigating."
            )
        path = qs.queue.pop(0)
        qs.current = path
        try:
            full = _resolve(path, workspace)
        except ValueError as e:
            qs.current = None
            return f"Error: {e}"
        if not os.path.isfile(full):
            qs.current = None
            return f"Error: file not found: {path} (was queued but no longer exists)"
        try:
            rendered, total = _read_numbered(full)
        except Exception as exc:
            qs.current = None
            return f"Error reading {path}: {exc}"
        trunc_note = (
            f"\n... ({total} total lines, showing first {_READ_LINE_CAP})"
            if total > _READ_LINE_CAP else ""
        )
        return (
            f'=== {path} ({total} lines) ===\n{rendered}{trunc_note}\n\n'
            f'Call note(path="{path}", start_line=X, end_line=Y, why=...) for the '
            "relevant section(s) before dequeuing the next file."
        )

    return dequeue_next_tool


def make_note_tool(qs: GatherQueueState):
    def note_tool(args: dict, workspace: str) -> str:
        path = args["path"]
        try:
            start_line = int(args["start_line"])
            end_line = int(args["end_line"])
        except (KeyError, TypeError, ValueError):
            return "Error: start_line and end_line are required integers."
        why = args.get("why", "")

        if path not in qs.seen:
            return f"Error: '{path}' was never enqueued/dequeued — nothing to note."
        try:
            full = _resolve(path, workspace)
        except ValueError as e:
            return f"Error: {e}"
        if not os.path.isfile(full):
            return f"Error: file not found: {path}"
        try:
            code, _total = _read_numbered(full, start_line, end_line)
        except Exception as exc:
            return f"Error reading {path}: {exc}"

        qs.notes.append({
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "code": code,
            "why": why,
        })
        if qs.current == path:
            qs.current = None
        return f"Noted {path} lines {start_line}-{end_line}."

    return note_tool


def build_gather_context_toolset(registry: ToolRegistry, qs: GatherQueueState) -> None:
    """Register the 3-tool read-queue set for the GATHER_CONTEXT subsession.

    run_shell is registered separately by the caller (agent/context.py-adjacent
    node code) since it's shared, stateless, and already defined in tools/shell.py.
    """
    registry.register(ToolSpec(
        name="enqueue",
        description=(
            "Add a file to your read-queue because you suspect it's relevant to the bug "
            "(e.g. found via grep as a caller/definition of something you just read). "
            "Files already queued or read are silently deduped. Capped at "
            f"{MAX_QUEUE_FILES} files total per session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from repo root"},
                "reason": {"type": "string", "description": "Why you think this file is relevant"},
            },
            "required": ["path", "reason"],
        },
        fn=make_enqueue_tool(qs),
    ))

    registry.register(ToolSpec(
        name="dequeue_next",
        description=(
            "Pop the next file off your read-queue and read it in full. Refuses to serve "
            "the next file until you've called note() on the current one. Call this first "
            "and repeatedly until the queue is empty."
        ),
        input_schema={"type": "object", "properties": {}},
        fn=make_dequeue_tool(qs),
    ))

    registry.register(ToolSpec(
        name="note",
        description=(
            "Record the bug-relevant section of the file you most recently dequeued (or any "
            "previously-read file). The harness re-reads the exact lines from disk — your "
            "job is to point at the right start_line/end_line and explain why, precisely, "
            "since this note (with its line numbers) becomes the IMPLEMENT agent's starting "
            "context. If nothing in the file is relevant, note it anyway with start_line=1, "
            "end_line=1, why=\"not relevant\" so you can move on."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "why": {"type": "string", "description": "Why this section matters to the bug (or 'not relevant')"},
            },
            "required": ["path", "start_line", "end_line", "why"],
        },
        fn=make_note_tool(qs),
    ))

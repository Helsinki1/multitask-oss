"""Tests for the stateful GATHER_CONTEXT read-queue tools (enqueue/dequeue_next/note)."""

from __future__ import annotations

from tools.gather_context_tools import (
    MAX_QUEUE_FILES,
    GatherQueueState,
    build_gather_context_toolset,
)
from tools.registry import ToolRegistry


def _make_registry(qs: GatherQueueState) -> ToolRegistry:
    reg = ToolRegistry()
    build_gather_context_toolset(reg, qs)
    return reg


def test_enqueue_dedupes(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    qs = GatherQueueState()
    reg = _make_registry(qs)

    r1 = reg.execute("enqueue", {"path": "a.py", "reason": "looks relevant"}, str(tmp_path))
    r2 = reg.execute("enqueue", {"path": "a.py", "reason": "again"}, str(tmp_path))

    assert "Enqueued" in r1
    assert "already queued" in r2
    assert qs.queue == ["a.py"]
    assert qs.seen == {"a.py"}


def test_enqueue_respects_cap(tmp_path):
    qs = GatherQueueState()
    reg = _make_registry(qs)
    for i in range(MAX_QUEUE_FILES):
        (tmp_path / f"f{i}.py").write_text("x = 1\n")
        reg.execute("enqueue", {"path": f"f{i}.py", "reason": "r"}, str(tmp_path))

    (tmp_path / "overflow.py").write_text("x = 1\n")
    result = reg.execute("enqueue", {"path": "overflow.py", "reason": "r"}, str(tmp_path))

    assert "cap" in result.lower()
    assert "overflow.py" not in qs.seen


def test_enqueue_rejects_missing_file(tmp_path):
    qs = GatherQueueState()
    reg = _make_registry(qs)
    result = reg.execute("enqueue", {"path": "nope.py", "reason": "r"}, str(tmp_path))
    assert "not found" in result.lower()


def test_dequeue_next_is_fifo_and_reads_full_file(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\nline3\n")
    (tmp_path / "b.py").write_text("only line\n")
    qs = GatherQueueState()
    qs.seed(["a.py", "b.py"])
    reg = _make_registry(qs)

    out = reg.execute("dequeue_next", {}, str(tmp_path))
    assert "a.py" in out
    assert "line1" in out and "line2" in out and "line3" in out
    assert qs.current == "a.py"
    assert qs.queue == ["b.py"]


def test_dequeue_next_blocked_until_current_is_noted(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    qs = GatherQueueState()
    qs.seed(["a.py", "b.py"])
    reg = _make_registry(qs)

    reg.execute("dequeue_next", {}, str(tmp_path))  # serves a.py
    blocked = reg.execute("dequeue_next", {}, str(tmp_path))
    assert "haven't called note()" in blocked
    assert qs.queue == ["b.py"]  # b.py was NOT popped

    reg.execute("note", {"path": "a.py", "start_line": 1, "end_line": 1, "why": "not relevant"}, str(tmp_path))
    unblocked = reg.execute("dequeue_next", {}, str(tmp_path))
    assert "b.py" in unblocked
    assert qs.current == "b.py"


def test_dequeue_next_empty_queue_message(tmp_path):
    qs = GatherQueueState()
    reg = _make_registry(qs)
    out = reg.execute("dequeue_next", {}, str(tmp_path))
    assert "empty" in out.lower()


def test_note_rereads_exact_lines_from_disk_ignoring_nothing_supplied(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    qs = GatherQueueState()
    qs.seed(["a.py"])
    reg = _make_registry(qs)

    reg.execute("dequeue_next", {}, str(tmp_path))
    reg.execute("note", {"path": "a.py", "start_line": 3, "end_line": 5, "why": "the bug is here"}, str(tmp_path))

    assert len(qs.notes) == 1
    note = qs.notes[0]
    assert note["path"] == "a.py"
    assert "line3" in note["code"]
    assert "line5" in note["code"]
    assert "line6" not in note["code"]
    assert note["why"] == "the bug is here"
    assert qs.current is None  # cleared after noting


def test_note_rejects_unseen_path(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    qs = GatherQueueState()
    reg = _make_registry(qs)
    result = reg.execute("note", {"path": "a.py", "start_line": 1, "end_line": 1, "why": "x"}, str(tmp_path))
    assert "never enqueued" in result
    assert qs.notes == []


def test_note_allows_multiple_notes_on_same_path(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    qs = GatherQueueState()
    qs.seed(["a.py"])
    reg = _make_registry(qs)

    reg.execute("dequeue_next", {}, str(tmp_path))
    reg.execute("note", {"path": "a.py", "start_line": 1, "end_line": 2, "why": "first"}, str(tmp_path))
    reg.execute("note", {"path": "a.py", "start_line": 8, "end_line": 9, "why": "second"}, str(tmp_path))

    assert len(qs.notes) == 2
    assert {n["why"] for n in qs.notes} == {"first", "second"}

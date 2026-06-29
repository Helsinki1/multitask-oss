"""Tests for the traceback-driven context gathering pipeline."""

from __future__ import annotations

from agent.context import (
    _find_local_callees,
    _parse_traceback,
    _render_outline_map,
    extract_file_outline,
)
from agent.state import TestCase, TestToDoList


def test_parse_traceback_standard_format(tmp_path):
    """Parses standard Python 'File "...", line N' format."""
    target = tmp_path / "mymodule.py"
    target.write_text("x = 1\n")

    output = f'  File "{target}", line 3, in some_func\n    raise ValueError\n'
    frames = _parse_traceback(output, str(tmp_path))

    assert len(frames) == 1
    assert frames[0][0] == str(target)
    assert frames[0][1] == 3


def test_parse_traceback_pytest_format(tmp_path):
    """Parses pytest short format 'path/to/file.py:N: in func'."""
    target = tmp_path / "test_foo.py"
    target.write_text("def test_x(): pass\n")

    output = f"{target}:5: in test_x\n    assert False\n"
    frames = _parse_traceback(output, str(tmp_path))

    assert any(f[0] == str(target) for f in frames)


def test_parse_traceback_ignores_outside_workspace(tmp_path):
    """Files outside the workspace are excluded."""
    output = 'File "/usr/lib/python3/dist-packages/foo.py", line 1, in bar\n'
    frames = _parse_traceback(output, str(tmp_path))
    assert frames == []


def test_todo_list_properties():
    cases = [
        TestCase("tests/test_a.py::test_one", "fail_to_pass", "failing", "tb1"),
        TestCase("tests/test_b.py::test_two", "fail_to_pass", "passing", ""),
        TestCase("tests/test_c.py::test_three", "pass_to_pass", "failing", "tb3"),
        TestCase("tests/test_d.py::test_four", "pass_to_pass", "passing", ""),
    ]
    todo = TestToDoList(cases=cases)

    assert len(todo.f2p_failing) == 1
    assert todo.f2p_failing[0].test_id == "tests/test_a.py::test_one"
    assert len(todo.p2p_failing) == 1
    assert todo.p2p_failing[0].test_id == "tests/test_c.py::test_three"
    assert not todo.all_f2p_pass
    assert not todo.all_pass


def test_todo_list_all_pass():
    cases = [
        TestCase("a::t1", "fail_to_pass", "passing"),
        TestCase("b::t2", "pass_to_pass", "passing"),
    ]
    todo = TestToDoList(cases=cases)
    assert todo.all_f2p_pass
    assert todo.all_pass


def test_extract_file_outline(tmp_path):
    """outline returns entries for top-level functions and class methods."""
    src = tmp_path / "mod.py"
    src.write_text(
        "class Foo:\n"
        "    def bar(self): pass\n"
        "    def baz(self): pass\n"
        "def helper(): pass\n"
    )
    outline = extract_file_outline(str(src))
    names = [e["name"] for e in outline]
    assert "Foo" in names
    assert "Foo.bar" in names
    assert "Foo.baz" in names
    assert "helper" in names


def test_find_local_callees(tmp_path):
    """_find_local_callees returns names of locally-defined functions called by frame."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def helper(): pass\n"
        "def external_call(): pass\n"
        "def frame_func():\n"
        "    helper()\n"
        "    unrelated = 1\n"
    )
    outline = extract_file_outline(str(src))
    frame_entry = next(e for e in outline if e["name"] == "frame_func")
    callees = _find_local_callees(str(src), [frame_entry["lineno"]], outline)
    assert "helper" in callees
    assert "frame_func" not in callees  # not a callee of itself


def test_render_outline_map_marks_frame_and_callee(tmp_path):
    """Navigation map marks traceback frames and callees correctly."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def helper(): pass\n"
        "def frame_func():\n"
        "    helper()\n"
    )
    outline = extract_file_outline(str(src))
    frame_entry = next(e for e in outline if e["name"] == "frame_func")
    helper_entry = next(e for e in outline if e["name"] == "helper")

    frame_lines = {frame_entry["lineno"]}
    callee_names = {"helper"}

    nav = _render_outline_map(outline, frame_lines, callee_names, "mod.py")
    assert "TRACEBACK FRAME" in nav
    assert "callee" in nav
    assert "helper" in nav


def test_default_implement_model():
    """Sanity-check that config defaults are set."""
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {}, clear=False):
        from cloud_agent.config import Settings
        s = Settings()
        assert s.implement_model  # non-empty
        assert s.discovery_model  # non-empty

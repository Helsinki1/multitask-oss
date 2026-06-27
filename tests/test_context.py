"""Tests for the new traceback-driven and dep-graph context gathering."""

from __future__ import annotations

from agent.context import (
    _parse_traceback,
    _build_import_subgraph,
    _dedupe_frames,
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


def test_dedupe_frames_source_before_test(tmp_path):
    """Source files come before test files in deduped output."""
    src = tmp_path / "mymodule.py"
    test = tmp_path / "tests" / "test_mymodule.py"
    test.parent.mkdir()
    src.write_text("x = 1\n")
    test.write_text("import mymodule\n")

    frames = [(str(src), 1), (str(test), 5)]
    result = _dedupe_frames(frames, str(tmp_path))

    src_rel = result.index(str(src))
    test_rel = result.index(str(test))
    assert src_rel < test_rel


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


def test_build_import_subgraph(tmp_path):
    """AST import walker resolves dotted module paths to workspace .py files."""
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    init = pkg / "__init__.py"
    init.write_text("")
    utils = pkg / "utils.py"
    utils.write_text("def helper(): pass\n")

    # 'from mypkg.utils import helper' → resolves module 'mypkg.utils' → mypkg/utils.py
    main = tmp_path / "main.py"
    main.write_text("from mypkg.utils import helper\n")

    found = _build_import_subgraph(str(tmp_path), [str(main)], hops=1)
    assert any("utils.py" in f for f in found)


def test_default_implement_model():
    """Sanity-check that config defaults are set."""
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {}, clear=False):
        from cloud_agent.config import Settings
        s = Settings()
        assert s.implement_model  # non-empty
        assert s.discovery_model  # non-empty

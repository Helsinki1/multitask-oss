"""Tests for the deterministic support code around context gathering:
traceback parsing, test-todo-list properties, and GATHER_CONTEXT read-queue seeding.

File selection/extraction itself is no longer algorithmic (see
blueprints/nodes/gather_context.py for the agentic read-queue subsession) —
these tests cover only what's still deterministic.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from agent.context import _dedupe_seed_paths, _parse_traceback, seed_gather_context
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


def test_dedupe_seed_paths_orders_source_before_test_and_dedupes():
    # Use a plain tempdir (not pytest's tmp_path fixture) — tmp_path's own directory
    # name embeds the test function name (contains "test"), which would make
    # _is_test_path misclassify every file under it regardless of filename.
    workspace = Path(tempfile.mkdtemp())
    src = workspace / "impl.py"
    src.write_text("x = 1\n")
    test_file = workspace / "test_impl.py"
    test_file.write_text("def test_x(): pass\n")

    frames = [(str(test_file), 1), (str(src), 5), (str(src), 9)]  # dup src, test first
    paths = _dedupe_seed_paths(str(workspace), frames)

    assert paths == ["impl.py", "test_impl.py"]  # source before test, deduped


def test_dedupe_seed_paths_skips_missing_files(tmp_path):
    frames = [(str(tmp_path / "does_not_exist.py"), 1)]
    assert _dedupe_seed_paths(str(tmp_path), frames) == []


def test_seed_gather_context_reuses_prior_todo_list_without_rerunning_tests(tmp_path):
    """On recontext re-entry, VERIFY's fresh todo_list must be reused as-is —
    seed_gather_context must NOT re-run tests (_run_and_collect must not be called)."""
    src = tmp_path / "impl.py"
    src.write_text("def f():\n    raise ValueError('boom')\n")

    prior = TestToDoList(cases=[
        TestCase(
            "test_impl.py::test_f", "fail_to_pass", "failing",
            f'  File "{src}", line 2, in f\n    raise ValueError\n',
        ),
    ])

    with patch("agent.context._run_and_collect") as mock_run:
        mock_run.side_effect = AssertionError("must not re-run tests on recontext re-entry")
        todo_list, seed_paths = seed_gather_context(
            str(tmp_path), ["test_impl.py::test_f"], [], prior_todo_list=prior,
        )

    mock_run.assert_not_called()
    assert todo_list is prior
    assert seed_paths == ["impl.py"]


def test_seed_gather_context_runs_tests_on_first_entry(tmp_path):
    """With no prior_todo_list (true first entry), seed_gather_context must run tests."""
    src = tmp_path / "impl.py"
    src.write_text("x = 1\n")
    fresh_todo = TestToDoList(cases=[TestCase("t::x", "fail_to_pass", "failing", "")])

    with patch("agent.context._run_and_collect") as mock_run, \
         patch("agent.context.resolve_test_ids", side_effect=lambda ws, ids: ids):
        mock_run.return_value = (fresh_todo, [(str(src), 1)])
        todo_list, seed_paths = seed_gather_context(
            str(tmp_path), ["t::x"], [], prior_todo_list=None,
        )

    mock_run.assert_called_once()
    assert todo_list is fresh_todo
    assert seed_paths == ["impl.py"]


def test_default_implement_model():
    """Sanity-check that config defaults are set."""
    import os

    with patch.dict(os.environ, {}, clear=False):
        from cloud_agent.config import Settings
        s = Settings()
        assert s.implement_model  # non-empty
        assert s.discovery_model  # non-empty

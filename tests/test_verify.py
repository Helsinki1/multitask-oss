"""Tests for VERIFY's deterministic routing, including the capped, targeted
recontext logic (f2p_new_error / p2p_regression -> GATHER_CONTEXT, capped at
MAX_RECONTEXT_ATTEMPTS).

Mocks tools.shell.run_cmd (via blueprints.nodes.verify.run_cmd) rather than
running real pytest — first mocked-subprocess precedent for a Node.run() test
in this repo; test_context.py's one existing unittest.mock usage patches
os.environ, not a subprocess.
"""

from __future__ import annotations

from unittest.mock import patch

from blueprints.nodes.verify import (
    MAX_RECONTEXT_ATTEMPTS,
    MAX_VERIFY_ATTEMPTS,
    VerifyNode,
    _exception_signature,
    _signature_changed,
)
from agent.state import AgentState, TestCase, TestToDoList


class _FakeTracer:
    def emit(self, *args, **kwargs) -> None:
        pass


def _state(workspace: str, f2p_traceback: str = "", f2p_status: str = "failing") -> AgentState:
    return AgentState(
        workspace_path=workspace,
        fail_to_pass=["t1"],
        pass_to_pass=["p1"],
        todo_list=TestToDoList(cases=[
            TestCase("t1", "fail_to_pass", f2p_status, f2p_traceback),
            TestCase("p1", "pass_to_pass", "passing", ""),
        ]),
    )


def _mock_run_cmd(f2p_output: tuple[int, str], p2p_output: tuple[int, str] = (0, "")):
    def side_effect(cmd, workspace, timeout):
        return f2p_output if "t1" in cmd else p2p_output
    return side_effect


# ── _exception_signature / _signature_changed ─────────────────────────────────


def test_exception_signature_extracts_trailing_error_token():
    assert _exception_signature("Traceback...\nE   TypeError: bad type") == "TypeError"
    assert _exception_signature("E   AssertionError: assert 1 == 2") == "AssertionError"


def test_exception_signature_empty_for_no_traceback():
    assert _exception_signature("") == ""


def test_exception_signature_empty_when_no_recognizable_exception_line():
    assert _exception_signature("assert False\nno exception marker here") == ""


def test_signature_changed_true_when_differs():
    class C:
        def __init__(self, tid, tb):
            self.test_id, self.traceback = tid, tb

    prior = {"t1": C("t1", "E   AssertionError: x")}
    failing = [C("t1", "E   TypeError: y")]
    assert _signature_changed(failing, prior) is True


def test_signature_changed_false_when_same():
    class C:
        def __init__(self, tid, tb):
            self.test_id, self.traceback = tid, tb

    prior = {"t1": C("t1", "E   AssertionError: x")}
    failing = [C("t1", "E   AssertionError: z")]
    assert _signature_changed(failing, prior) is False


# ── VerifyNode.run routing ─────────────────────────────────────────────────────


def test_verify_passes_routes_to_checkpoint(tmp_path):
    state = _state(str(tmp_path))
    node = VerifyNode(tracer=_FakeTracer())
    with patch("blueprints.nodes.verify.run_cmd", side_effect=_mock_run_cmd((0, ""))):
        result = node.run(state)
    assert result.next_node == "CHECKPOINT"
    assert result.state_update["verify_failure_type"] == ""
    assert result.status == "ok"


def test_verify_p2p_regression_routes_to_gather_context(tmp_path):
    state = _state(str(tmp_path), f2p_status="passing")
    node = VerifyNode(tracer=_FakeTracer())
    with patch("blueprints.nodes.verify.run_cmd", side_effect=_mock_run_cmd((0, ""), (1, "E   AssertionError: regressed"))):
        result = node.run(state)
    assert result.next_node == "GATHER_CONTEXT"
    assert result.state_update["verify_failure_type"] == "p2p_regression"
    assert result.state_update["recontext_attempts"] == 1
    assert "regression" in result.state_update["recontext_reason"].lower()


def test_verify_f2p_new_error_routes_to_gather_context_with_reason(tmp_path):
    state = _state(str(tmp_path), f2p_traceback="E   AssertionError: x")
    node = VerifyNode(tracer=_FakeTracer())
    with patch("blueprints.nodes.verify.run_cmd", side_effect=_mock_run_cmd((1, "E   TypeError: y"))):
        result = node.run(state)
    assert result.next_node == "GATHER_CONTEXT"
    assert result.state_update["verify_failure_type"] == "f2p_new_error"
    assert result.state_update["recontext_attempts"] == 1
    assert "AssertionError -> TypeError" in result.state_update["recontext_reason"]


def test_verify_f2p_same_signature_routes_to_implement_not_gather_context(tmp_path):
    """Repeated same-signature failure (the is_upper oscillation case) must NOT
    trigger a recontext — it should keep retrying IMPLEMENT directly."""
    state = _state(str(tmp_path), f2p_traceback="E   AssertionError: x")
    node = VerifyNode(tracer=_FakeTracer())
    with patch("blueprints.nodes.verify.run_cmd", side_effect=_mock_run_cmd((1, "E   AssertionError: x"))):
        result = node.run(state)
    assert result.next_node == "IMPLEMENT"
    assert result.state_update["verify_failure_type"] == "f2p_failing"
    assert result.state_update["recontext_attempts"] == 0
    assert result.state_update["recontext_reason"] == ""


def test_recontext_attempts_cap_falls_back_to_implement(tmp_path):
    """After MAX_RECONTEXT_ATTEMPTS gather-worthy failures, VERIFY must stop
    routing to GATHER_CONTEXT and fall back to plain IMPLEMENT retries."""
    node = VerifyNode(tracer=_FakeTracer())
    state = _state(str(tmp_path), f2p_status="passing")  # drive via p2p_regression each round

    seen_next_nodes = []
    sigs = ["E   AssertionError: v0"]
    for round_ in range(MAX_RECONTEXT_ATTEMPTS + 2):
        p2p_tb = f"E   AssertionError: regressed v{round_}"
        with patch("blueprints.nodes.verify.run_cmd", side_effect=_mock_run_cmd((0, ""), (1, p2p_tb))):
            result = node.run(state)
        seen_next_nodes.append(result.next_node)
        state = state.apply_update(result.state_update)
        if state.verify_attempts >= MAX_VERIFY_ATTEMPTS:
            break

    gather_count = seen_next_nodes.count("GATHER_CONTEXT")
    assert gather_count == MAX_RECONTEXT_ATTEMPTS
    assert state.recontext_attempts == MAX_RECONTEXT_ATTEMPTS
    # once capped, further gather-worthy failures still route somewhere sane (not stuck)
    assert all(n in ("GATHER_CONTEXT", "IMPLEMENT", "CHECKPOINT") for n in seen_next_nodes)


def test_max_verify_attempts_exhaustion_routes_to_checkpoint_regardless(tmp_path):
    node = VerifyNode(tracer=_FakeTracer())
    state = _state(str(tmp_path), f2p_traceback="E   AssertionError: x")
    for _ in range(MAX_VERIFY_ATTEMPTS):
        with patch("blueprints.nodes.verify.run_cmd", side_effect=_mock_run_cmd((1, "E   AssertionError: x"))):
            result = node.run(state)
        state = state.apply_update(result.state_update)
    assert result.next_node == "CHECKPOINT"
    assert state.verify_attempts == MAX_VERIFY_ATTEMPTS

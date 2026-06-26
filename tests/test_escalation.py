"""Tests for the deterministic escalation policy."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, call, patch

import pytest

from agent.escalation import (
    EscalationConfig,
    EscalationMetrics,
    _is_blocked_text,
    _is_tool_failure,
    build_escalation_message,
    record_tool_result,
    should_escalate,
)
from agent.state import AgentState, BudgetState
from agent.subsession import _token_limit_kwargs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(**kwargs) -> EscalationConfig:
    defaults = dict(default_model="gpt-4o-mini", escalated_model="gpt-4o")
    defaults.update(kwargs)
    return EscalationConfig(**defaults)


def _metrics(**kwargs) -> EscalationMetrics:
    m = EscalationMetrics(current_model="gpt-4o-mini")
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _state() -> AgentState:
    return AgentState(
        task_text="write a hello world function",
        workspace_path="/tmp/repo",
    )


# ---------------------------------------------------------------------------
# Test 1: no escalation on an easy, successful task
# ---------------------------------------------------------------------------

class TestNoEscalationOnSuccess:
    def test_fresh_metrics_early_turn(self):
        cfg = _config()
        met = _metrics()
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert not triggered

    def test_no_escalation_when_disabled(self):
        cfg = _config(escalation_enabled=False)
        met = _metrics(failed_completion_checks=99, repeated_tool_failure_count=99)
        triggered, _ = should_escalate(cfg, met, turn=90, max_turns=100)
        assert not triggered

    def test_no_escalation_same_model(self):
        """If default and escalated are the same model, escalation is a no-op."""
        cfg = _config(default_model="gpt-4o", escalated_model="gpt-4o")
        met = _metrics(failed_completion_checks=5)
        triggered, _ = should_escalate(cfg, met, turn=50, max_turns=100)
        assert not triggered

    def test_successful_tool_resets_failure_count(self):
        cfg = _config(escalation_after_repeated_tool_failures=2)
        met = _metrics()
        # First failure
        record_tool_result(met, "run_shell", {"command": "pytest"}, "$ pytest\nexit code: 1\nFAIL")
        assert met.repeated_tool_failure_count == 1
        # Success resets it
        record_tool_result(met, "run_shell", {"command": "pytest"}, "$ pytest\nexit code: 0\nPASSED")
        assert met.repeated_tool_failure_count == 0
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert not triggered


# ---------------------------------------------------------------------------
# Test 2: escalation after repeated failed completion checks
# ---------------------------------------------------------------------------

class TestEscalationAfterFailedCompletionChecks:
    def test_triggers_at_threshold(self):
        cfg = _config(escalation_after_failed_completion_checks=2)
        met = _metrics(failed_completion_checks=2)
        triggered, reason = should_escalate(cfg, met, turn=0, max_turns=100)
        assert triggered
        assert "completion checks" in reason
        assert "2" in reason

    def test_does_not_trigger_below_threshold(self):
        cfg = _config(escalation_after_failed_completion_checks=2)
        met = _metrics(failed_completion_checks=1)
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert not triggered

    def test_threshold_of_one(self):
        cfg = _config(escalation_after_failed_completion_checks=1)
        met = _metrics(failed_completion_checks=1)
        triggered, reason = should_escalate(cfg, met, turn=0, max_turns=100)
        assert triggered

    def test_reason_contains_count(self):
        cfg = _config(escalation_after_failed_completion_checks=3)
        met = _metrics(failed_completion_checks=5)
        triggered, reason = should_escalate(cfg, met, turn=0, max_turns=100)
        assert triggered
        assert "5" in reason


# ---------------------------------------------------------------------------
# Test 3: escalation after repeated identical failing shell/test command
# ---------------------------------------------------------------------------

class TestEscalationAfterRepeatedToolFailures:
    def test_same_command_twice_triggers(self):
        cfg = _config(escalation_after_repeated_tool_failures=2)
        met = _metrics()
        result = "$ python3 -m pytest\nexit code: 1\nTraceback (most recent call last):\n  ..."
        record_tool_result(met, "run_shell", {"command": "python3 -m pytest"}, result)
        record_tool_result(met, "run_shell", {"command": "python3 -m pytest"}, result)
        triggered, reason = should_escalate(cfg, met, turn=0, max_turns=100)
        assert triggered
        assert "tool failures" in reason

    def test_different_commands_do_not_chain(self):
        """Two different failing commands do not count as repeated failures of the same."""
        cfg = _config(escalation_after_repeated_tool_failures=2)
        met = _metrics()
        record_tool_result(met, "run_shell", {"command": "pytest"}, "exit code: 1")
        record_tool_result(met, "run_shell", {"command": "make build"}, "exit code: 2")
        assert met.repeated_tool_failure_count == 1  # restarted counter for new key
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert not triggered

    def test_success_between_failures_resets(self):
        cfg = _config(escalation_after_repeated_tool_failures=2)
        met = _metrics()
        record_tool_result(met, "run_shell", {"command": "pytest"}, "exit code: 1")
        record_tool_result(met, "run_shell", {"command": "pytest"}, "exit code: 0\n1 passed")
        record_tool_result(met, "run_shell", {"command": "pytest"}, "exit code: 1")
        assert met.repeated_tool_failure_count == 1  # only 1 consecutive failure after reset
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert not triggered

    def test_traceback_counts_as_failure(self):
        cfg = _config(escalation_after_repeated_tool_failures=2)
        met = _metrics()
        tb = "Traceback (most recent call last):\n  File 'x.py'\nValueError: bad"
        record_tool_result(met, "run_shell", {"command": "python x.py"}, tb)
        record_tool_result(met, "run_shell", {"command": "python x.py"}, tb)
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert triggered

    def test_run_tests_tool_failures_tracked(self):
        cfg = _config(escalation_after_repeated_tool_failures=2)
        met = _metrics()
        result = "$ pytest\nexit code: 1\npytest: 0 passed, 3 failed"
        record_tool_result(met, "run_tests", {"command": "pytest"}, result)
        record_tool_result(met, "run_tests", {"command": "pytest"}, result)
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert triggered


# ---------------------------------------------------------------------------
# Test 4: escalation event appears in the trace
# ---------------------------------------------------------------------------

class TestEscalationTraceEvent:
    """Integration test: run_subsession with mocked OpenAI triggers escalation and emits trace."""

    def _make_text_response(self, content: str = "Making progress on the task."):
        """Mock OpenAI response with no tool calls (triggers completion check)."""
        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.message.content = content
        choice.message.tool_calls = None
        response = MagicMock()
        response.choices = [choice]
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        return response

    def test_escalation_event_emitted_after_completion_check_failures(self):
        from agent.subsession import SubsessionConfig, run_subsession
        from agent.escalation import EscalationConfig
        from tools.registry import ToolRegistry

        cfg = EscalationConfig(
            default_model="gpt-4o-mini",
            escalated_model="gpt-4o",
            escalation_after_failed_completion_checks=2,
            escalation_after_turn_fraction=0,  # disable turn-fraction to isolate completion-check trigger
        )
        sub_config = SubsessionConfig(
            name="test",
            system_prompt="You are a coding agent.",
            initial_human_message="Fix the bug.",
            model="gpt-4o-mini",
            max_turns=10,
            escalation_config=cfg,
        )
        state = _state()
        registry = ToolRegistry()
        tracer = MagicMock()

        not_done_output = MagicMock()
        not_done_output.is_done = False
        not_done_output.confidence = "low"
        not_done_output.reason = "tests not run"
        not_done_output.missing_steps = ["run tests"]

        done_output = MagicMock()
        done_output.is_done = True
        done_output.confidence = "high"
        done_output.reason = "task complete"
        done_output.missing_steps = []

        text_response = self._make_text_response("Analyzing the codebase.")

        with (
            patch("agent.subsession.openai.OpenAI") as mock_openai_cls,
            patch("agent.subsession.check_is_done") as mock_done,
        ):
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            # 3 text responses: turns 0, 1 → escalation after turn 1; turn 2 → done
            mock_client.chat.completions.create.return_value = text_response
            mock_done.side_effect = [not_done_output, not_done_output, done_output]

            result, _ = run_subsession(sub_config, state, registry, tracer)

        # Verify the model.escalated event was emitted
        emitted_types = [c.args[0] for c in tracer.emit.call_args_list]
        assert "model.escalated" in emitted_types

        # Find the escalation event and verify its payload
        esc_call = next(c for c in tracer.emit.call_args_list if c.args[0] == "model.escalated")
        payload = esc_call.args[1]
        assert payload["old_model"] == "gpt-4o-mini"
        assert payload["new_model"] == "gpt-4o"
        assert "completion checks" in payload["reason"]
        assert "turn" in payload
        assert "cost_so_far" in payload

    def test_escalation_message_injected_into_conversation(self):
        from agent.subsession import SubsessionConfig, run_subsession
        from agent.escalation import EscalationConfig
        from tools.registry import ToolRegistry

        cfg = EscalationConfig(
            default_model="gpt-4o-mini",
            escalated_model="gpt-4o",
            escalation_after_failed_completion_checks=1,
            escalation_after_turn_fraction=0,
        )
        sub_config = SubsessionConfig(
            name="test",
            system_prompt="system",
            initial_human_message="task",
            model="gpt-4o-mini",
            max_turns=5,
            escalation_config=cfg,
        )
        state = _state()
        registry = ToolRegistry()
        tracer = MagicMock()

        not_done = MagicMock(is_done=False, confidence="low", reason="not done", missing_steps=[])
        done = MagicMock(is_done=True, confidence="high", reason="done", missing_steps=[])
        text_response = self._make_text_response("thinking...")

        with (
            patch("agent.subsession.openai.OpenAI") as mock_openai_cls,
            patch("agent.subsession.check_is_done") as mock_done,
        ):
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = text_response
            mock_done.side_effect = [not_done, done]

            result, _ = run_subsession(sub_config, state, registry, tracer)

        # The escalated model must be used in subsequent calls
        calls = mock_client.chat.completions.create.call_args_list
        # First call uses default model, later calls use escalated model
        assert calls[0].kwargs["model"] == "gpt-4o-mini"
        assert calls[1].kwargs["model"] == "gpt-4o"

        # The escalation context message must appear in the conversation
        final_messages = calls[1].kwargs["messages"]
        esc_messages = [m for m in final_messages if "[ESCALATION" in (m.get("content") or "")]
        assert len(esc_messages) == 1


# ---------------------------------------------------------------------------
# Test 5: model switches only once when max_escalations=1
# ---------------------------------------------------------------------------

class TestMaxEscalations:
    def test_no_second_escalation_after_first(self):
        cfg = _config(max_escalations=1, escalation_after_failed_completion_checks=1)
        met = _metrics(failed_completion_checks=1)
        # First escalation
        triggered, reason = should_escalate(cfg, met, turn=0, max_turns=100)
        assert triggered
        met.escalation_count += 1  # simulate escalation performed
        # Second attempt — must not trigger
        met.failed_completion_checks += 1
        triggered, _ = should_escalate(cfg, met, turn=0, max_turns=100)
        assert not triggered

    def test_max_escalations_zero_means_never(self):
        cfg = _config(max_escalations=0)
        met = _metrics(failed_completion_checks=99, repeated_tool_failure_count=99)
        triggered, _ = should_escalate(cfg, met, turn=90, max_turns=100)
        assert not triggered

    def test_model_switches_exactly_once_in_subsession(self):
        """Integration: with max_escalations=1, model switches once and stays switched."""
        from agent.subsession import SubsessionConfig, run_subsession
        from agent.escalation import EscalationConfig
        from tools.registry import ToolRegistry

        cfg = EscalationConfig(
            default_model="gpt-4o-mini",
            escalated_model="gpt-4o",
            max_escalations=1,
            escalation_after_failed_completion_checks=1,
            escalation_after_turn_fraction=0,
        )
        sub_config = SubsessionConfig(
            name="test",
            system_prompt="system",
            initial_human_message="task",
            model="gpt-4o-mini",
            max_turns=8,
            escalation_config=cfg,
        )
        state = _state()
        registry = ToolRegistry()
        tracer = MagicMock()

        not_done = MagicMock(is_done=False, confidence="low", reason="x", missing_steps=[])
        done = MagicMock(is_done=True, confidence="high", reason="done", missing_steps=[])

        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.message.content = "still working"
        choice.message.tool_calls = None
        response = MagicMock()
        response.choices = [choice]
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50

        with (
            patch("agent.subsession.openai.OpenAI") as mock_openai_cls,
            patch("agent.subsession.check_is_done") as mock_done,
        ):
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = response
            # 4 not-done checks, then done
            mock_done.side_effect = [not_done, not_done, not_done, not_done, done]

            result, _ = run_subsession(sub_config, state, registry, tracer)

        # Count model.escalated events — must be exactly 1
        escalation_events = [
            c for c in tracer.emit.call_args_list if c.args[0] == "model.escalated"
        ]
        assert len(escalation_events) == 1

        # All calls after the first must use the escalated model
        api_calls = mock_client.chat.completions.create.call_args_list
        models_used = [c.kwargs["model"] for c in api_calls]
        # First call uses default; rest use escalated
        assert models_used[0] == "gpt-4o-mini"
        assert all(m == "gpt-4o" for m in models_used[1:])


# ---------------------------------------------------------------------------
# Additional unit tests for helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_token_limit_kwargs_uses_max_completion_tokens_for_gpt5(self):
        assert _token_limit_kwargs("gpt-5.4-mini", 8192) == {"max_completion_tokens": 8192}

    def test_token_limit_kwargs_keeps_max_tokens_for_gpt4o(self):
        assert _token_limit_kwargs("gpt-4o-mini", 8192) == {"max_tokens": 8192}

    def test_is_tool_failure_exit_code_1(self):
        assert _is_tool_failure("$ pytest\nexit code: 1\nFAIL")

    def test_is_tool_failure_exit_code_127(self):
        assert _is_tool_failure("$ python\nexit code: 127\n")

    def test_is_tool_failure_exit_code_0_not_failure(self):
        assert not _is_tool_failure("$ pytest\nexit code: 0\n2 passed")

    def test_is_tool_failure_traceback(self):
        assert _is_tool_failure("Traceback (most recent call last):\n  File x.py\nValueError")

    def test_is_blocked_text_detects_stuck(self):
        assert _is_blocked_text("I'm stuck and cannot proceed with this task.")

    def test_is_blocked_text_detects_unable(self):
        assert _is_blocked_text("I am unable to run the tests in this environment.")

    def test_is_blocked_text_normal_message(self):
        assert not _is_blocked_text("I've written the function and tests pass.")

    def test_turn_fraction_escalation(self):
        cfg = _config(escalation_after_turn_fraction=0.5, escalation_after_failed_completion_checks=100)
        met = _metrics()
        assert not should_escalate(cfg, met, turn=49, max_turns=100)[0]
        triggered, reason = should_escalate(cfg, met, turn=50, max_turns=100)
        assert triggered
        assert "turn threshold" in reason

    def test_build_escalation_message_contains_task(self):
        msg = build_escalation_message(
            task_text="implement login",
            reason="repeated tool failures: 2",
            turn=5,
            cost_so_far=0.012,
            messages=[],
            escalated_model="gpt-4o",
        )
        assert "implement login" in msg
        assert "repeated tool failures" in msg
        assert "gpt-4o" in msg
        assert "$0.0120" in msg

    def test_build_escalation_message_extracts_commands(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{
                    "function": {
                        "name": "run_shell",
                        "arguments": json.dumps({"command": "pytest tests/"}),
                    }
                }],
            },
            {"role": "tool", "content": "$ pytest tests/\nexit code: 1\nFAIL"},
        ]
        msg = build_escalation_message("task", "reason", 3, 0.01, messages, "gpt-4o")
        assert "pytest tests/" in msg
        assert "Recent failures" in msg

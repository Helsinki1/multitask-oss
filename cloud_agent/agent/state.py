from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid8() -> str:
    return str(uuid.uuid4())[:8]


@dataclass
class BudgetState:
    max_llm_turns: int = 100
    used_llm_turns: int = 0
    max_wall_seconds: int = 7200
    elapsed_seconds: float = 0.0
    max_cost_usd: float = 10.0
    used_cost_usd: float = 0.0
    max_tool_calls: int = 500
    used_tool_calls: int = 0

    def copy_with(self, **kwargs: object) -> "BudgetState":
        import copy
        b = copy.copy(self)
        for k, v in kwargs.items():
            setattr(b, k, v)
        return b


@dataclass
class ContextBundle:
    repo_summary: str = ""
    task_summary: str = ""
    repo_rules: list[str] = field(default_factory=list)
    coding_standards: list[str] = field(default_factory=list)
    build_and_test_commands: list[str] = field(default_factory=list)
    repo_map: str = ""
    task_adjacent_files: list[dict] = field(default_factory=list)


@dataclass
class NodeError:
    node: str
    message: str
    exception_type: Optional[str] = None
    retryable: bool = False
    timestamp: datetime = field(default_factory=_now)


@dataclass
class AgentState:
    # Identity
    task_id: str = field(default_factory=_uuid8)
    session_id: str = field(default_factory=_uuid8)
    user_id: str = "local"

    # Repo
    repo_url: str = ""
    repo_name: str = ""
    base_branch: str = "main"
    working_branch: str = ""
    is_public_repo: bool = False

    # Sandbox
    workspace_path: str = ""
    sandbox_backend: str = "local"

    # Task
    task_text: str = ""
    task_source: str = "cli"
    context_bundle: ContextBundle = field(default_factory=ContextBundle)

    # Git
    initial_commit_sha: Optional[str] = None
    merge_base_sha: Optional[str] = None
    changed_files: list[str] = field(default_factory=list)

    # Orchestration
    current_node: str = "01_CHECK_BRANCH"
    implementation_done: bool = False
    task_status: str = "running"  # "running" | "done" | "failed" | "cancelled"

    # Validation
    lint_status: str = "not_run"
    test_status: str = "not_run"
    policy_status: str = "not_run"
    push_status: str = "not_run"

    # Output
    pr_url: Optional[str] = None
    branch_url: Optional[str] = None

    # Flags
    run_ci_loop: bool = False
    run_related_tests: bool = False
    only_implement: bool = True  # default: implement only, no push/PR
    allow_push: bool = False

    # Resources
    budgets: BudgetState = field(default_factory=BudgetState)
    errors: list[NodeError] = field(default_factory=list)

    def apply_update(self, update: dict) -> "AgentState":
        import copy
        s = copy.copy(self)
        for k, v in update.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def with_node(self, node: str) -> "AgentState":
        return self.apply_update({"current_node": node})

    def with_status(self, status: str) -> "AgentState":
        return self.apply_update({"task_status": status})

    def add_error(self, error: NodeError) -> "AgentState":
        return self.apply_update({"errors": self.errors + [error]})

    def to_dict(self) -> dict:
        import json
        from dataclasses import asdict

        def _serialize(obj: object) -> object:
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj

        raw = asdict(self)

        def _walk(obj: object) -> object:
            if isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(i) for i in obj]
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj

        return _walk(raw)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentState":
        from datetime import datetime

        budgets_d = d.pop("budgets", {})
        cb_d = d.pop("context_bundle", {})
        errors_d = d.pop("errors", [])

        budgets = BudgetState(**budgets_d) if budgets_d else BudgetState()
        cb = ContextBundle(**cb_d) if cb_d else ContextBundle()
        errors = [
            NodeError(
                node=e["node"],
                message=e["message"],
                exception_type=e.get("exception_type"),
                retryable=e.get("retryable", False),
                timestamp=datetime.fromisoformat(e["timestamp"]) if e.get("timestamp") else _now(),
            )
            for e in errors_d
        ]

        return cls(budgets=budgets, context_bundle=cb, errors=errors, **d)

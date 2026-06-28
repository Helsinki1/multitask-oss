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
class TestCase:
    """One test the harness must run. Updated after each VERIFY pass."""
    test_id: str
    category: str       # "fail_to_pass" | "pass_to_pass"
    status: str         # "failing" | "passing" | "unknown"
    traceback: str = "" # last captured pytest output for this test


@dataclass
class TestToDoList:
    """Deterministic record of test status — never LLM-populated."""
    cases: list[TestCase] = field(default_factory=list)

    @property
    def f2p_failing(self) -> list[TestCase]:
        return [c for c in self.cases if c.category == "fail_to_pass" and c.status != "passing"]

    @property
    def p2p_failing(self) -> list[TestCase]:
        return [c for c in self.cases if c.category == "pass_to_pass" and c.status == "failing"]

    @property
    def all_f2p_pass(self) -> bool:
        f2p = [c for c in self.cases if c.category == "fail_to_pass"]
        return bool(f2p) and all(c.status == "passing" for c in f2p)

    @property
    def all_pass(self) -> bool:
        return bool(self.cases) and all(c.status == "passing" for c in self.cases)

    def summary(self) -> str:
        f2p = [c for c in self.cases if c.category == "fail_to_pass"]
        p2p = [c for c in self.cases if c.category == "pass_to_pass"]
        f2p_pass = sum(1 for c in f2p if c.status == "passing")
        p2p_pass = sum(1 for c in p2p if c.status == "passing")
        lines = [f"fail_to_pass: {f2p_pass}/{len(f2p)} passing"]
        if p2p:
            lines.append(f"pass_to_pass: {p2p_pass}/{len(p2p)} passing")
        return "  ".join(lines)


@dataclass
class ContextBundle:
    repo_rules: list[str] = field(default_factory=list)
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

    # Sandbox
    workspace_path: str = ""
    sandbox_backend: str = "local"

    # Task
    task_text: str = ""
    task_source: str = "cli"
    task_type: str = "bug_fix"  # "bug_fix" | "additive" — set by LOAD_TASK
    context_bundle: ContextBundle = field(default_factory=ContextBundle)

    # Git
    initial_commit_sha: Optional[str] = None
    working_branch_sha: Optional[str] = None  # SHA before first implementation attempt

    # Orchestration
    current_node: str = "CHECK_BRANCH"
    task_status: str = "running"  # "running" | "done" | "failed" | "cancelled"

    # Structured test to-do list (updated deterministically by GATHER_CONTEXT / VERIFY)
    todo_list: TestToDoList = field(default_factory=TestToDoList)

    # Retry state
    verify_attempts: int = 0
    verify_failure_type: str = ""  # "f2p_failing" | "p2p_regression" | ""
    baseline_p2p_failing: list[str] = field(default_factory=list)  # p2p tests failing BEFORE any agent changes

    # Eval mode (SWE-bench etc.)
    eval_mode: bool = False
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)

    # Additive path: path to the contract test file written by DEFINE_CONTRACT
    contract_test_path: str = ""

    # Output
    pr_url: Optional[str] = None
    branch_url: Optional[str] = None
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
        from dataclasses import asdict

        def _walk(obj: object) -> object:
            if isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(i) for i in obj]
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj

        return _walk(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> "AgentState":
        budgets_d = d.pop("budgets", {})
        cb_d = d.pop("context_bundle", {})
        errors_d = d.pop("errors", [])
        todo_d = d.pop("todo_list", {})

        budgets = BudgetState(**budgets_d) if budgets_d else BudgetState()
        cb = ContextBundle(**{k: v for k, v in cb_d.items() if k in ContextBundle.__dataclass_fields__}) if cb_d else ContextBundle()
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
        cases = [TestCase(**c) for c in todo_d.get("cases", [])]
        todo_list = TestToDoList(cases=cases)

        return cls(
            budgets=budgets,
            context_bundle=cb,
            errors=errors,
            todo_list=todo_list,
            **d,
        )

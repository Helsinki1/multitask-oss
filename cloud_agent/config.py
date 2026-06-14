import os


class Settings:
    openai_api_key: str
    db_path: str
    traces_dir: str
    implement_model: str
    checker_model: str
    pr_description_model: str
    protected_branches: list[str]

    # Escalation
    escalated_model: str
    escalation_enabled: bool
    max_escalations: int
    escalation_after_failed_completion_checks: int
    escalation_after_repeated_tool_failures: int
    escalation_after_turn_fraction: float

    def __init__(self) -> None:
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.db_path = os.environ.get("DB_PATH", "cloud_agent.db")
        self.traces_dir = os.environ.get("TRACES_DIR", "traces")
        self.implement_model = os.environ.get("IMPLEMENT_MODEL", "codex-5.2")
        self.checker_model = os.environ.get("CHECKER_MODEL", "gpt-4o-mini")
        self.pr_description_model = os.environ.get("PR_DESC_MODEL", "gpt-4o-mini")
        self.protected_branches = ["main", "master", "develop", "production", "release"]

        self.escalated_model = os.environ.get("ESCALATED_MODEL", "codex-5.2")
        self.escalation_enabled = os.environ.get("ESCALATION_ENABLED", "true").lower() == "true"
        self.max_escalations = int(os.environ.get("MAX_ESCALATIONS", "1"))
        self.escalation_after_failed_completion_checks = int(
            os.environ.get("ESCALATION_AFTER_FAILED_CHECKS", "2")
        )
        self.escalation_after_repeated_tool_failures = int(
            os.environ.get("ESCALATION_AFTER_TOOL_FAILURES", "2")
        )
        self.escalation_after_turn_fraction = float(
            os.environ.get("ESCALATION_TURN_FRACTION", "0.4")
        )


settings = Settings()

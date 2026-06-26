import os


class Settings:
    openai_api_key: str
    db_path: str
    traces_dir: str
    discovery_model: str
    implement_model: str
    checker_model: str
    pr_description_model: str
    protected_branches: list[str]

    # Docker + sandbox
    docker_image: str
    agent_workspace: str

    # Reproduce-first
    repro_max_turns: int

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
        self.discovery_model = os.environ.get("DISCOVERY_MODEL", "gpt-4o-mini")
        self.implement_model = os.environ.get("IMPLEMENT_MODEL", "gpt-5.4-mini")
        self.checker_model = os.environ.get("CHECKER_MODEL", "gpt-4o-mini")
        self.pr_description_model = os.environ.get("PR_DESC_MODEL", "gpt-4o-mini")
        self.protected_branches = ["main", "master", "develop", "production", "release"]

        self.docker_image = os.environ.get("AGENT_DOCKER_IMAGE", "python:3.10-slim")
        self.agent_workspace = os.environ.get("AGENT_WORKSPACE", "")
        self.repro_max_turns = int(os.environ.get("REPRO_MAX_TURNS", "15"))

        self.escalated_model = os.environ.get("ESCALATED_MODEL", "gpt-5.5")
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

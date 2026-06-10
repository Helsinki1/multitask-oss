import os


class Settings:
    anthropic_api_key: str
    db_path: str
    traces_dir: str
    implement_model: str
    checker_model: str
    pr_description_model: str
    protected_branches: list[str]

    def __init__(self) -> None:
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.db_path = os.environ.get("DB_PATH", "cloud_agent.db")
        self.traces_dir = os.environ.get("TRACES_DIR", "traces")
        self.implement_model = os.environ.get("IMPLEMENT_MODEL", "claude-opus-4-8")
        self.checker_model = os.environ.get("CHECKER_MODEL", "claude-haiku-4-5-20251001")
        self.pr_description_model = os.environ.get("PR_DESC_MODEL", "claude-haiku-4-5-20251001")
        self.protected_branches = ["main", "master", "develop", "production", "release"]


settings = Settings()

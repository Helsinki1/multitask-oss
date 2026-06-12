import os


class Settings:
    openai_api_key: str
    db_path: str
    traces_dir: str
    implement_model: str
    checker_model: str
    pr_description_model: str
    protected_branches: list[str]

    def __init__(self) -> None:
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.db_path = os.environ.get("DB_PATH", "cloud_agent.db")
        self.traces_dir = os.environ.get("TRACES_DIR", "traces")
        self.implement_model = os.environ.get("IMPLEMENT_MODEL", "gpt-4o")
        self.checker_model = os.environ.get("CHECKER_MODEL", "gpt-4o-mini")
        self.pr_description_model = os.environ.get("PR_DESC_MODEL", "gpt-4o-mini")
        self.protected_branches = ["main", "master", "develop", "production", "release"]


settings = Settings()

import os


class Settings:
    openai_api_key: str
    db_path: str
    traces_dir: str
    discovery_model: str
    implement_model: str
    protected_branches: list[str]

    def __init__(self) -> None:
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.db_path = os.environ.get("DB_PATH", "cloud_agent.db")
        self.traces_dir = os.environ.get("TRACES_DIR", "traces")
        self.discovery_model = os.environ.get("DISCOVERY_MODEL", "gpt-4o-mini")
        self.implement_model = os.environ.get("IMPLEMENT_MODEL", "gpt-5.4-mini")
        self.protected_branches = ["main", "master", "develop", "production", "release"]


settings = Settings()

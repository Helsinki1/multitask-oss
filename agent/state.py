import json
import os
from datetime import datetime, timezone
from typing import TypedDict

TRACES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "traces")


class AgentState(TypedDict):
    task: str
    repo_path: str
    iteration: int
    max_iterations: int
    file_listing: list[str]
    files_read: dict[str, str]
    plan: str
    patches_attempted: list[str]
    patch_errors: list[str]
    test_runs: list[dict]
    test_passed: bool
    messages: list
    summary: str
    done: bool


def log_event(event: str, **kwargs) -> None:
    os.makedirs(TRACES_DIR, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **kwargs,
    }
    path = os.path.join(TRACES_DIR, "actions.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

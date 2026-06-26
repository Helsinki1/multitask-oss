"""SQLite state store for tasks, sessions, and node runs."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from agent.state import AgentState
from cloud_agent.config import settings


DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    channel     TEXT,
    source      TEXT,
    repo_url    TEXT,
    base_branch TEXT,
    task_text   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    task_id        TEXT REFERENCES tasks(id),
    working_branch TEXT,
    current_node   TEXT,
    state_json     TEXT,
    started_at     TEXT,
    ended_at       TEXT,
    status         TEXT
);

CREATE TABLE IF NOT EXISTS node_runs (
    id           TEXT PRIMARY KEY,
    session_id   TEXT REFERENCES sessions(id),
    node_name    TEXT NOT NULL,
    node_type    TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    status       TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_sessions_task  ON sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_noderuns_sess  ON node_runs(session_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


class StateStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.db_path
        self._init_db()

    def _init_db(self) -> None:
        with _conn(self.db_path) as con:
            con.executescript(DDL)

    def save(self, state: AgentState) -> None:
        now = _now_iso()
        state_json = json.dumps(state.to_dict())

        with _conn(self.db_path) as con:
            # Upsert task row
            con.execute(
                """
                INSERT INTO tasks (id, user_id, channel, source, repo_url, base_branch,
                                   task_text, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status     = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    state.task_id,
                    state.user_id,
                    "cli",
                    state.task_source,
                    state.repo_url,
                    state.base_branch,
                    state.task_text,
                    state.task_status,
                    now,
                    now,
                ),
            )
            # Upsert session row
            con.execute(
                """
                INSERT INTO sessions (id, task_id, working_branch, current_node,
                                      state_json, started_at, status)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    working_branch = excluded.working_branch,
                    current_node   = excluded.current_node,
                    state_json     = excluded.state_json,
                    status         = excluded.status
                """,
                (
                    state.session_id,
                    state.task_id,
                    state.working_branch,
                    state.current_node,
                    state_json,
                    now,
                    state.task_status,
                ),
            )

    def load(self, session_id: str) -> Optional[AgentState]:
        with _conn(self.db_path) as con:
            row = con.execute(
                "SELECT state_json FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return AgentState.from_dict(json.loads(row["state_json"]))

    def record_node_run(
        self,
        session_id: str,
        node_name: str,
        node_type: str,
        started_at: str,
        ended_at: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        import uuid

        with _conn(self.db_path) as con:
            con.execute(
                """
                INSERT INTO node_runs (id, session_id, node_name, node_type,
                                       started_at, ended_at, status, error)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4())[:8],
                    session_id,
                    node_name,
                    node_type,
                    started_at,
                    ended_at,
                    status,
                    error,
                ),
            )

    def finalize_session(self, session_id: str, status: str) -> None:
        now = _now_iso()
        with _conn(self.db_path) as con:
            con.execute(
                "UPDATE sessions SET ended_at = ?, status = ? WHERE id = ?",
                (now, status, session_id),
            )

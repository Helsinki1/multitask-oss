from __future__ import annotations

from dataclasses import dataclass

from agent.context import build_context_bundle, fetch_remote_agents_files
from agent.prompts import build_implement_human
from agent.state import AgentState, ContextBundle
from cloud_agent.config import Settings


def test_build_context_bundle_preloads_task_adjacent_files(tmp_path, monkeypatch):
    src = tmp_path / "cloud_agent" / "agent"
    src.mkdir(parents=True)
    context_file = src / "context.py"
    context_file.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")
    unrelated_file = tmp_path / "README.md"
    unrelated_file.write_text("hello", encoding="utf-8")

    monkeypatch.setattr("agent.context.fetch_remote_agents_files", lambda *_: [])

    cb = build_context_bundle(
        str(tmp_path),
        "Change cloud_agent/agent/context.py and add task_adjacent_files",
    )

    assert cb.task_adjacent_files
    first = cb.task_adjacent_files[0]
    assert first["path"] == "cloud_agent/agent/context.py"
    assert first["score"] > 0
    assert "line 149" in first["content"]
    assert "line 150" not in first["content"]


def test_build_implement_human_includes_preloaded_files_with_cap():
    cb = ContextBundle(
        repo_map="cloud_agent/agent/context.py: build_context_bundle",
        task_adjacent_files=[
            {
                "path": "cloud_agent/agent/context.py",
                "score": 3.0,
                "content": "x" * 13000,
            }
        ],
    )
    state = AgentState(task_text="update context", context_bundle=cb)

    prompt = build_implement_human(state)

    assert "Likely relevant files (pre-loaded for you):" in prompt
    assert '<file path="cloud_agent/agent/context.py">' in prompt
    assert len(prompt.split('<file path="cloud_agent/agent/context.py">', 1)[1]) < 12500


def test_fetch_remote_agents_files_uses_github_raw_url(monkeypatch):
    calls: list[str] = []

    def fake_run_git(args, workspace):
        if args == ["remote", "get-url", "origin"]:
            return "git@github.com:org/repo.git"
        if args == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        return ""

    @dataclass
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size):
            return b"remote rules"

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr("agent.context._run_git", fake_run_git)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    contents = fetch_remote_agents_files("task", "/tmp/workspace")

    assert contents == ["remote rules", "remote rules", "remote rules"]
    assert calls[0] == "https://raw.githubusercontent.com/org/repo/main/AGENTS.md"


def test_default_implement_model_is_current_codex_model_not_opus(monkeypatch):
    monkeypatch.delenv("IMPLEMENT_MODEL", raising=False)
    monkeypatch.delenv("ESCALATED_MODEL", raising=False)

    settings = Settings()

    assert settings.implement_model == "gpt-5.4-mini"
    assert settings.escalated_model == "gpt-5.5"
    assert "opus" not in settings.implement_model
    assert "opus" not in settings.escalated_model

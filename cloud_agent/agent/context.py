"""PREPARE_CONTEXT logic: build a ContextBundle from the repo."""

import os
import re
import subprocess
import urllib.error
import urllib.request

from cloud_agent.agent.state import ContextBundle


_RULE_FILES = ["AGENTS.md", "CLAUDE.md", ".cursor/rules", ".github/copilot-instructions.md"]
_REMOTE_RULE_FILES = ["AGENTS.md", ".github/AGENTS.md", "docs/AGENTS.md"]
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", "dist", "build"}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "if",
    "in", "into", "is", "it", "of", "on", "or", "the", "this", "to", "up", "use",
    "with", "you", "your",
}


def build_context_bundle(workspace: str, task_text: str) -> ContextBundle:
    cb = ContextBundle(task_summary=task_text)

    # 1. Repository rules from AGENTS.md / CLAUDE.md etc.
    for fname in _RULE_FILES:
        path = os.path.join(workspace, fname)
        if os.path.exists(path):
            try:
                content = open(path, encoding="utf-8").read(8000)
                cb.repo_rules.append(f"[from {fname}]\n{content}")
            except OSError:
                pass

    for content in fetch_remote_agents_files(task_text, workspace):
        cb.repo_rules.append(f"[from remote AGENTS.md]\n{content}")

    # 2. Build/test commands from manifest files
    cb.build_and_test_commands = _detect_build_commands(workspace)

    # 3. Lightweight repo map
    cb.repo_map = _generate_repo_map(workspace)
    cb.task_adjacent_files = _find_task_adjacent_files(workspace, task_text, cb.repo_map)

    return cb


def fetch_remote_agents_files(task_text: str, workspace: str) -> list[str]:
    """Best-effort fetch of remote AGENTS.md-style rules from GitHub."""
    del task_text  # Reserved for future filtering; keep API task-aware.
    try:
        remote = _run_git(["remote", "get-url", "origin"], workspace)
        repo = _parse_github_repo(remote)
        if not repo:
            return []

        org, name = repo
        contents: list[str] = []
        for branch in _candidate_remote_branches(workspace):
            for rule_path in _REMOTE_RULE_FILES:
                url = f"https://raw.githubusercontent.com/{org}/{name}/{branch}/{rule_path}"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "cloud-agent"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = resp.read(4000)
                    contents.append(data.decode("utf-8", errors="replace")[:4000])
                except (OSError, urllib.error.URLError, UnicodeError):
                    continue
            if contents:
                break
        return contents
    except Exception:
        return []


def _detect_build_commands(workspace: str) -> list[str]:
    cmds: list[str] = []

    # pyproject.toml / setup.cfg -> pytest
    if os.path.exists(os.path.join(workspace, "pyproject.toml")):
        try:
            content = open(os.path.join(workspace, "pyproject.toml")).read()
            if "pytest" in content:
                cmds.append("pytest")
            else:
                cmds.append("python -m pytest")
        except OSError:
            cmds.append("python -m pytest")

    # Makefile
    mf = os.path.join(workspace, "Makefile")
    if os.path.exists(mf):
        try:
            for line in open(mf).readlines()[:60]:
                if line.startswith(("test", "check")):
                    target = line.split(":")[0].strip()
                    cmds.append(f"make {target}")
                    break
        except OSError:
            pass

    # package.json
    pj = os.path.join(workspace, "package.json")
    if os.path.exists(pj):
        try:
            import json
            data = json.loads(open(pj).read())
            scripts = data.get("scripts", {})
            if "test" in scripts:
                cmds.append(f"npm test  # runs: {scripts['test']}")
        except Exception:
            cmds.append("npm test")

    # go.mod
    if os.path.exists(os.path.join(workspace, "go.mod")):
        cmds.append("go test ./...")

    # Default: try pytest for any .py files
    if not cmds:
        py_files = [f for f in os.listdir(workspace) if f.endswith(".py")]
        if py_files:
            cmds.append("python3 -m pytest")

    return cmds


def _generate_repo_map(workspace: str) -> str:
    from cloud_agent.tools.search import get_repo_map_tool
    try:
        return get_repo_map_tool({}, workspace)
    except Exception:
        return "(repo map unavailable)"


def _run_git(args: list[str], workspace: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_github_repo(remote_url: str) -> tuple[str, str] | None:
    patterns = [
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, remote_url.strip())
        if match:
            return match.group(1), match.group(2)
    return None


def _candidate_remote_branches(workspace: str) -> list[str]:
    branches: list[str] = []
    head = _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], workspace)
    prefix = "refs/remotes/origin/"
    if head.startswith(prefix):
        branches.append(head[len(prefix):])
    for branch in ("main", "master"):
        if branch not in branches:
            branches.append(branch)
    return branches


def _find_task_adjacent_files(workspace: str, task_text: str, repo_map: str) -> list[dict]:
    keywords = _task_keywords(task_text)
    if not keywords:
        return []

    repo_map_files = set(_extract_repo_map_files(repo_map))
    repo_files = set(_iter_repo_files(workspace))
    files = sorted(repo_files | {path for path in repo_map_files if path in repo_files})
    scored: list[tuple[float, str]] = []
    for rel_path in files:
        score = _score_path(rel_path, keywords)
        if score > 0:
            scored.append((score, rel_path))

    scored.sort(key=lambda item: (-item[0], item[1]))
    adjacent: list[dict] = []
    for score, rel_path in scored[:5]:
        content = _read_file_lines(os.path.join(workspace, rel_path), max_lines=150)
        if content:
            adjacent.append({"path": rel_path, "score": score, "content": content})
    return adjacent


def _task_keywords(task_text: str) -> set[str]:
    raw_tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", task_text)
    explicit_paths = re.findall(r"[\w./-]+\.[A-Za-z0-9]+", task_text)
    tokens: set[str] = set()

    for token in raw_tokens:
        for part in _split_identifier(token):
            lowered = part.lower()
            if len(lowered) > 1 and lowered not in _STOPWORDS:
                tokens.add(lowered)

    for path in explicit_paths:
        tokens.add(path.lower())
        _, ext = os.path.splitext(path)
        if ext:
            tokens.add(ext.lstrip(".").lower())
        for part in re.split(r"[/._-]+", path):
            lowered = part.lower()
            if len(lowered) > 1 and lowered not in _STOPWORDS:
                tokens.add(lowered)

    return tokens


def _split_identifier(token: str) -> list[str]:
    pieces: list[str] = []
    for snake_part in re.split(r"_+", token):
        pieces.extend(re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", snake_part))
    return pieces or [token]


def _extract_repo_map_files(repo_map: str) -> list[str]:
    files: set[str] = set()
    for line in repo_map.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("===") or stripped.endswith("/"):
            continue
        candidate = stripped.split(":", 1)[0].strip()
        candidate = candidate.split(" ", 1)[0].strip()
        if "." in os.path.basename(candidate):
            files.add(candidate)
    return sorted(files)


def _iter_repo_files(workspace: str) -> list[str]:
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for filename in sorted(filenames):
            full_path = os.path.join(dirpath, filename)
            paths.append(os.path.relpath(full_path, workspace))
    return paths


def _score_path(rel_path: str, keywords: set[str]) -> float:
    lowered_path = rel_path.lower()
    filename = os.path.basename(lowered_path)
    dirname = os.path.dirname(lowered_path)
    score = 0.0
    for keyword in keywords:
        if keyword in filename:
            score += 2.0
        elif keyword in dirname:
            score += 1.0
        elif keyword in lowered_path:
            score += 1.0
    return score


def _read_file_lines(path: str, max_lines: int) -> str:
    try:
        lines: list[str] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f):
                if idx >= max_lines:
                    break
                lines.append(line)
        return "".join(lines)
    except OSError:
        return ""

"""PREPARE_CONTEXT logic: build a ContextBundle from the repo."""

import ast
import os
import re
import subprocess
import urllib.error
import urllib.request

from agent.state import ContextBundle


_RULE_FILES = ["AGENTS.md", "CLAUDE.md", ".cursor/rules", ".github/copilot-instructions.md"]
_REMOTE_RULE_FILES = ["AGENTS.md", ".github/AGENTS.md", "docs/AGENTS.md"]
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", "dist", "build"}


def build_context_bundle(
    workspace: str,
    task_text: str,
    fail_to_pass: list[str] | None = None,
) -> ContextBundle:
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

    # 4. Task-adjacent files — two strategies depending on mode:
    #    eval mode  : run failing tests → parse traceback → follow import graph
    #    normal mode: single LLM call that reads the repo map and selects files
    if fail_to_pass:
        cb.task_adjacent_files = _find_task_adjacent_files_eval(workspace, fail_to_pass)
    else:
        selections = _gather_context_with_llm(workspace, task_text, cb.repo_map)
        adjacent = []
        for sel in selections[:7]:
            path = sel.get("path", "")
            full = os.path.join(workspace, path)
            if not os.path.isfile(full):
                continue
            content = _read_file_lines(full, max_lines=150)
            if content:
                adjacent.append({"path": path, "why": sel.get("why", ""), "content": content})
        cb.task_adjacent_files = adjacent

    return cb


# ── Normal mode: LLM-based file selection ────────────────────────────────────


def _gather_context_with_llm(workspace: str, task_text: str, repo_map: str) -> list[dict]:
    """Single cheap LLM call: read the repo map, return [{path, why}] for relevant files.

    Keeps LLM calls out of the main agent loop for the exploration work. Returns
    path+why only — caller reads content so file I/O stays in one place.
    Falls back to [] on any failure (non-fatal: agent can explore on its own).
    """
    try:
        import json
        import openai
        from cloud_agent.config import settings

        system = """\
You are a repository file selector. Given a task and a repo file listing, identify \
the 3-7 source files most likely to need reading or editing to complete the task.

Output ONLY a JSON array — no markdown fences, no explanation:
[{"path": "relative/path.py", "why": "one-sentence reason"}, ...]"""

        human = f"Task: {task_text}\n\nRepository files:\n{repo_map[:6000]}"

        client = openai.OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.discovery_model,
            temperature=0,
            max_tokens=600,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": human},
            ],
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown fences if the model adds them despite instructions
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
        return json.loads(raw)
    except Exception:
        return []


# ── Eval mode: traceback-driven file discovery ────────────────────────────────


def _find_task_adjacent_files_eval(workspace: str, fail_to_pass: list[str]) -> list[dict]:
    """Eval mode: run failing tests → parse error traceback → follow import graph.

    The traceback tells us exactly which files were executing when the test
    broke. Those are the causal files, not a keyword guess. We then follow
    their import graph one hop to catch transitive dependencies (e.g. a base
    class defined in a module imported by the failing module).
    """
    # Run the tests and capture the full output including traceback
    error_output = _run_failing_tests(workspace, fail_to_pass)

    # Parse traceback frames: (absolute_path, line_number)
    frames = _parse_traceback(error_output, workspace)

    if not frames:
        # Fallback: no runnable tests yet → trace imports from test files statically
        return _find_files_from_test_imports(workspace, fail_to_pass)

    # Deduplicate preserving order; non-test files first, test files at end
    seen: set[str] = set()
    source_frames: list[tuple[str, int]] = []
    test_frames: list[tuple[str, int]] = []
    for path, lineno in frames:
        if path in seen:
            continue
        seen.add(path)
        parts = path.replace("\\", "/").split("/")
        if any("test" in p.lower() for p in parts):
            test_frames.append((path, lineno))
        else:
            source_frames.append((path, lineno))

    ordered_frames = source_frames + test_frames
    seed_files = [p for p, _ in ordered_frames][:6]
    seed_set = set(seed_files)

    # One import-graph hop from each seed file to catch transitive dependencies
    imported = _build_import_subgraph(workspace, seed_files, hops=1)
    all_files = seed_files + [f for f in imported if f not in seed_set]

    result: list[dict] = []
    for filepath in all_files[:8]:
        lines_for_file = [ln for p, ln in ordered_frames if p == filepath]
        center = lines_for_file[0] if lines_for_file else 1
        start, end = _find_range_around_line(filepath, center)
        content = _read_file_range(filepath, start, end)
        if not content:
            continue
        why = (
            "appears in error traceback"
            if filepath in seed_set
            else f"imported by {os.path.relpath(seed_files[0], workspace)}"
        )
        result.append({
            "path": os.path.relpath(filepath, workspace),
            "line_start": start,
            "line_end": end,
            "content": content,
            "why": why,
        })

    return result


def _find_files_from_test_imports(workspace: str, fail_to_pass: list[str]) -> list[dict]:
    """Fallback when tests cannot be run: trace AST imports from test files."""
    test_files = []
    for tid in fail_to_pass:
        file_part = tid.split("::")[0]
        full = os.path.join(workspace, file_part)
        if os.path.isfile(full):
            test_files.append(full)
    if not test_files:
        return []

    imported = _build_import_subgraph(workspace, test_files, hops=1)
    result = []
    for filepath in imported[:6]:
        content = _read_file_lines(filepath, max_lines=150)
        if content:
            result.append({
                "path": os.path.relpath(filepath, workspace),
                "content": content,
                "why": "imported by failing test",
            })
    return result


def _run_failing_tests(workspace: str, fail_to_pass: list[str]) -> str:
    """Run up to 3 failing test IDs and return combined stdout+stderr."""
    test_ids = fail_to_pass[:3]
    try:
        r = subprocess.run(
            ["python", "-m", "pytest"] + test_ids + ["--tb=long", "-x", "--no-header", "-q"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return r.stdout + r.stderr
    except Exception:
        return ""


def _parse_traceback(output: str, workspace: str) -> list[tuple[str, int]]:
    """Extract (absolute_path, line_number) pairs from Python/pytest traceback output.

    Handles both:
    - Standard Python:  File "path/to/file.py", line N, in func
    - pytest short:     path/to/file.py:N: in func
    """
    frames: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def _add(path: str, lineno: int) -> None:
        if not os.path.isabs(path):
            path = os.path.join(workspace, path)
        # Only include files that actually exist inside the workspace
        try:
            rel = os.path.relpath(path, workspace)
        except ValueError:
            return
        if rel.startswith(".."):
            return
        if os.path.isfile(path):
            key = (path, lineno)
            if key not in seen:
                seen.add(key)
                frames.append(key)

    # Pattern 1: standard Python traceback
    for m in re.finditer(r'File "([^"]+)", line (\d+)', output):
        _add(m.group(1), int(m.group(2)))

    # Pattern 2: pytest short/long format  path/to/file.py:N: in func
    for m in re.finditer(r'^(\S[^\n:]+\.py):(\d+): in ', output, re.MULTILINE):
        _add(m.group(1), int(m.group(2)))

    return frames


def _build_import_subgraph(
    workspace: str, seed_files: list[str], hops: int = 1
) -> list[str]:
    """Walk import statements from seed_files, returning workspace source files found.

    Uses AST parsing so it works without executing any code. Only follows imports
    that resolve to actual .py files inside the workspace (stdlib excluded).
    """
    found: list[str] = []
    seen: set[str] = set(seed_files)
    frontier = list(seed_files)

    for _ in range(hops):
        next_frontier: list[str] = []
        for filepath in frontier:
            try:
                source = open(filepath, encoding="utf-8", errors="ignore").read()
                tree = ast.parse(source)
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    cand = _module_to_file(workspace, node.module)
                    if cand and cand not in seen:
                        seen.add(cand)
                        found.append(cand)
                        next_frontier.append(cand)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        cand = _module_to_file(workspace, alias.name)
                        if cand and cand not in seen:
                            seen.add(cand)
                            found.append(cand)
                            next_frontier.append(cand)
        frontier = next_frontier

    return found


def _module_to_file(workspace: str, module: str) -> str | None:
    """Resolve a dotted module name to a .py file inside the workspace, or None."""
    # sympy.core.symbol → sympy/core/symbol.py
    direct = os.path.join(workspace, module.replace(".", os.sep) + ".py")
    if os.path.isfile(direct):
        return direct
    # sympy.core → sympy/core/__init__.py
    pkg = os.path.join(workspace, module.replace(".", os.sep), "__init__.py")
    if os.path.isfile(pkg):
        return pkg
    return None


def _find_range_around_line(filepath: str, center_line: int, context: int = 80) -> tuple[int, int]:
    """Find the enclosing function/class body around center_line.

    Walks backward from center_line to find the nearest def/class header,
    then forward to the next same-level definition. Caps at context lines
    so we don't dump a 2000-line class body.
    """
    try:
        lines = open(filepath, encoding="utf-8", errors="ignore").readlines()
    except OSError:
        half = context // 2
        return max(1, center_line - half), center_line + half

    n = len(lines)
    center_line = max(1, min(center_line, n))

    # Walk backward to find nearest def/class at the same or lower indent
    start = center_line
    for i in range(center_line - 2, max(-1, center_line - 60), -1):
        stripped = lines[i].strip()
        if stripped.startswith(("def ", "class ", "async def ")):
            start = i + 1  # 1-indexed
            break

    # Walk forward to find the next top-level boundary
    indent0 = len(lines[start - 1]) - len(lines[start - 1].lstrip()) if start <= n else 0
    end = min(n, start + context)
    for i in range(start, min(n, start + context)):
        if i >= n:
            break
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            continue
        curr_indent = len(line) - len(line.lstrip())
        if i > start - 1 and curr_indent <= indent0 and line.strip().startswith(
            ("def ", "class ", "async def ")
        ):
            end = i  # exclusive → line i not included
            break

    return start, min(end, start + context)


def _read_file_range(filepath: str, start: int, end: int) -> str:
    """Read lines [start, end] (1-indexed, inclusive)."""
    try:
        lines = open(filepath, encoding="utf-8", errors="ignore").readlines()
        return "".join(lines[start - 1: end])
    except OSError:
        return ""


# ── Shared infrastructure (unchanged) ────────────────────────────────────────


def fetch_remote_agents_files(task_text: str, workspace: str) -> list[str]:
    """Best-effort fetch of remote AGENTS.md-style rules from GitHub."""
    del task_text
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

    if os.path.exists(os.path.join(workspace, "pyproject.toml")):
        try:
            content = open(os.path.join(workspace, "pyproject.toml")).read()
            cmds.append("pytest" if "pytest" in content else "python -m pytest")
        except OSError:
            cmds.append("python -m pytest")

    mf = os.path.join(workspace, "Makefile")
    if os.path.exists(mf):
        try:
            for line in open(mf).readlines()[:60]:
                if line.startswith(("test", "check")):
                    cmds.append(f"make {line.split(':')[0].strip()}")
                    break
        except OSError:
            pass

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

    if os.path.exists(os.path.join(workspace, "go.mod")):
        cmds.append("go test ./...")

    if not cmds:
        py_files = [f for f in os.listdir(workspace) if f.endswith(".py")]
        if py_files:
            tests_dir = os.path.join(workspace, "tests")
            scope = "tests/" if os.path.isdir(tests_dir) else "."
            cmds.append(f"python3 -m pytest {scope} -x -q")

    return cmds


def _generate_repo_map(workspace: str) -> str:
    from tools.search import get_repo_map_tool
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
    return result.stdout.strip() if result.returncode == 0 else ""


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

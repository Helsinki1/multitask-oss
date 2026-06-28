"""Algorithmic context gathering — traceback-driven (bug fix) and dep-graph (additive).

No LLM calls. All context is derived deterministically from test output and AST analysis.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess

from agent.state import ContextBundle, TestCase, TestToDoList


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", "dist", "build"}
_RULE_FILES = ["AGENTS.md", "CLAUDE.md", ".cursor/rules", ".github/copilot-instructions.md"]


# ── Test ID resolution ────────────────────────────────────────────────────────


def _is_full_test_id(test_id: str) -> bool:
    """Return True if test_id is already a runnable pytest node ID (no resolution needed)."""
    return "::" in test_id or ("/" in test_id and test_id.endswith(".py"))


def resolve_test_id(workspace: str, test_id: str, preferred_file: str = "") -> str:
    """Convert a bare/Django-style test ID to a full pytest node ID.

    SWE-bench supplies test IDs in multiple formats:
      - Full:   sympy/core/tests/test_basic.py::test_foo   (use as-is)
      - Bare:   test_foo                                    (search test files)
      - Django: test_foo (module.path.ClassName)            (parse + map to file)

    preferred_file: if provided, check this file first before searching globally.
    """
    if _is_full_test_id(test_id):
        return test_id

    # Django format: "test_name (dotted.module.ClassName)"
    m = re.match(r"^(\w+)\s+\(([^)]+)\)$", test_id.strip())
    if m:
        func_name = m.group(1)
        dotted = m.group(2)  # e.g. "model_fields.test_filepathfield.FilePathFieldTests"
        parts = dotted.rsplit(".", 1)
        if len(parts) == 2:
            module_dotted, class_name = parts
            file_rel = module_dotted.replace(".", "/") + ".py"
            for prefix in ("", "tests/", "test/"):
                candidate = os.path.join(workspace, prefix + file_rel)
                if os.path.isfile(candidate):
                    rel = os.path.relpath(candidate, workspace)
                    return f"{rel}::{class_name}::{func_name}"
        return f"-k {func_name}"

    # Bare function name: check preferred_file first, then search globally
    if "/" not in test_id and not test_id.endswith(".py"):
        if preferred_file:
            abs_pref = os.path.join(workspace, preferred_file)
            try:
                content = open(abs_pref, errors="ignore").read()
                if re.search(rf"^\s*def {re.escape(test_id)}\s*\(", content, re.MULTILINE):
                    return f"{preferred_file}::{test_id}"
            except OSError:
                pass

        try:
            r = subprocess.run(
                ["grep", "-r", f"def {test_id}", "--include=*.py", "-l", "."],
                capture_output=True, text=True, cwd=workspace, timeout=30,
            )
            candidates = [
                line.strip() for line in r.stdout.splitlines()
                if "test" in line.lower() and line.strip().endswith(".py")
            ]
            if candidates:
                return f"{candidates[0]}::{test_id}"
        except Exception:
            pass

    return test_id


def resolve_test_ids(workspace: str, test_ids: list[str]) -> list[str]:
    """Resolve a batch of test IDs, using majority-vote file detection to handle ambiguity.

    When bare function names could match multiple test files, we find the single
    file that contains the most of the given test functions, then use that file
    for all bare-name IDs. This avoids picking the wrong file when common names
    like 'test_equality' appear in many test files.
    """
    if not test_ids:
        return test_ids

    # Separate already-resolved from bare IDs
    bare_ids = [tid for tid in test_ids if not _is_full_test_id(tid) and
                not re.match(r"^(\w+)\s+\(", tid.strip())]  # exclude Django-style too

    preferred_file = ""
    if bare_ids:
        preferred_file = _find_best_file_for_bare_ids(workspace, bare_ids)

    return [resolve_test_id(workspace, tid, preferred_file=preferred_file) for tid in test_ids]


def _find_best_file_for_bare_ids(workspace: str, bare_ids: list[str]) -> str:
    """Find the single test file that contains the most of the given bare test IDs.

    Runs ONE grep per function name and tallies which file appears most often.
    """
    from collections import Counter
    file_counts: Counter = Counter()

    for tid in bare_ids:
        try:
            r = subprocess.run(
                ["grep", "-r", f"def {tid}", "--include=*.py", "-l", "."],
                capture_output=True, text=True, cwd=workspace, timeout=15,
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if "test" in line.lower() and line.endswith(".py"):
                    file_counts[line] += 1
        except Exception:
            pass

    return file_counts.most_common(1)[0][0] if file_counts else ""


# ── Bug fix: traceback-driven context ────────────────────────────────────────


def build_bugfix_context(
    workspace: str,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
) -> tuple[TestToDoList, ContextBundle]:
    """Run failing tests, parse tracebacks, load causally-adjacent source files.

    Returns the initial TestToDoList (all tests run once) and a ContextBundle
    whose task_adjacent_files come from traceback frames + one import-graph hop.
    """
    # Resolve bare/Django test IDs using ALL test IDs for stronger majority voting
    all_resolved = resolve_test_ids(workspace, fail_to_pass + pass_to_pass)
    fail_to_pass = all_resolved[:len(fail_to_pass)]
    pass_to_pass = all_resolved[len(fail_to_pass):]

    cb = ContextBundle()
    cb.repo_rules = _load_repo_rules(workspace)
    cb.build_and_test_commands = _detect_build_commands(workspace)
    cb.repo_map = _generate_repo_map(workspace)

    todo_list, tb_files = _run_and_collect(workspace, fail_to_pass, pass_to_pass)
    cb.task_adjacent_files = _load_context_files(workspace, tb_files)

    return todo_list, cb


def rebuild_context_from_regressions(
    workspace: str,
    todo_list: TestToDoList,
) -> ContextBundle:
    """Re-derive context from regression tracebacks for a p2p retry.

    Called by GATHER_CONTEXT when VERIFY detects pass_to_pass failures.
    Parses the traceback already stored in each failing p2p TestCase.
    """
    cb = ContextBundle()
    cb.repo_rules = _load_repo_rules(workspace)
    cb.build_and_test_commands = _detect_build_commands(workspace)

    all_tracebacks = "\n".join(
        c.traceback for c in todo_list.cases if c.status == "failing" and c.traceback
    )
    frames = _parse_traceback(all_tracebacks, workspace)
    tb_files = _dedupe_frames(frames, workspace)
    cb.task_adjacent_files = _load_context_files(workspace, tb_files)

    return cb


def run_tests_update_todo(
    workspace: str,
    todo_list: TestToDoList,
) -> TestToDoList:
    """Re-run all tests in todo_list and return a new list with updated statuses."""
    updated: list[TestCase] = []
    for case in todo_list.cases:
        out = _run_single_test(workspace, case.test_id)
        passed = out.strip() and "passed" in out and "failed" not in out and "error" not in out.lower()
        # More reliable: check exit code via subprocess directly
        rc = _test_exit_code(workspace, case.test_id)
        updated.append(TestCase(
            test_id=case.test_id,
            category=case.category,
            status="passing" if rc == 0 else "failing",
            traceback=out if rc != 0 else "",
        ))
    return TestToDoList(cases=updated)


# ── Additive: algorithmic dep-graph context ───────────────────────────────────


def build_additive_context(workspace: str, task_text: str) -> ContextBundle:
    """Build context for additive tasks using repo map + import dependency graph.

    No LLM. Finds the most-imported modules (hottest nodes in the dep graph)
    and the files most likely touched by a task matching the task text keywords.
    """
    cb = ContextBundle()
    cb.repo_rules = _load_repo_rules(workspace)
    cb.build_and_test_commands = _detect_build_commands(workspace)
    cb.repo_map = _generate_repo_map(workspace)

    # Build full import graph across the workspace
    all_py = _find_python_files(workspace)
    import_graph = _build_full_import_graph(workspace, all_py)

    # Score files by: (a) how many others import them, (b) keyword match with task
    keywords = set(re.sub(r"[^a-z0-9]", " ", task_text.lower()).split())
    scored: list[tuple[float, str]] = []
    for filepath in all_py:
        rel = os.path.relpath(filepath, workspace)
        in_degree = import_graph.get(filepath, {}).get("imported_by_count", 0)
        kw_score = sum(1 for kw in keywords if kw in rel.lower() or kw in filepath.lower())
        score = in_degree * 0.3 + kw_score * 2.0
        if score > 0:
            scored.append((score, filepath))

    scored.sort(reverse=True)
    top_files = [fp for _, fp in scored[:8]]

    result: list[dict] = []
    for filepath in top_files:
        content = _read_file_lines(filepath, max_lines=150)
        if content:
            in_count = import_graph.get(filepath, {}).get("imported_by_count", 0)
            result.append({
                "path": os.path.relpath(filepath, workspace),
                "content": content,
                "why": f"imported by {in_count} other modules" if in_count else "keyword match",
            })

    cb.task_adjacent_files = result
    return cb


# ── Internal: test running ────────────────────────────────────────────────────


def _run_and_collect(
    workspace: str,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
) -> tuple[TestToDoList, list[str]]:
    """Run all tests, build initial TestToDoList, return list of traceback-source files."""
    cases: list[TestCase] = []
    all_tb_text = ""

    for tid in fail_to_pass:
        rc, out = _run_pytest(workspace, [tid], flags=["--tb=long", "-x", "--no-header", "-q"])
        status = "passing" if rc == 0 else "failing"
        cases.append(TestCase(test_id=tid, category="fail_to_pass", status=status, traceback=out))
        if rc != 0:
            all_tb_text += f"\n{out}"

    for tid in pass_to_pass:
        rc, out = _run_pytest(workspace, [tid], flags=["--tb=short", "--no-header", "-q"])
        status = "passing" if rc == 0 else "failing"
        cases.append(TestCase(test_id=tid, category="pass_to_pass", status=status, traceback=out if rc != 0 else ""))

    frames = _parse_traceback(all_tb_text, workspace)
    tb_files = _dedupe_frames(frames, workspace)

    return TestToDoList(cases=cases), tb_files


def _run_pytest(workspace: str, test_ids: list[str], flags: list[str] | None = None) -> tuple[int, str]:
    cmd = ["python", "-m", "pytest"] + (flags or []) + test_ids
    try:
        r = subprocess.run(
            cmd, cwd=workspace, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PAGER": "cat", "TQDM_DISABLE": "1", "NO_COLOR": "1"},
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:
        return 1, str(exc)


def _run_single_test(workspace: str, test_id: str) -> str:
    _, out = _run_pytest(workspace, [test_id], flags=["--tb=long", "-x", "--no-header", "-q"])
    return out


def _test_exit_code(workspace: str, test_id: str) -> int:
    rc, _ = _run_pytest(workspace, [test_id], flags=["-x", "--no-header", "-q", "--tb=no"])
    return rc


# ── Internal: traceback parsing ───────────────────────────────────────────────


def _parse_traceback(output: str, workspace: str) -> list[tuple[str, int]]:
    """Extract (absolute_path, line_number) pairs from pytest/Python traceback output."""
    frames: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def _add(path: str, lineno: int) -> None:
        if not os.path.isabs(path):
            path = os.path.join(workspace, path)
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

    for m in re.finditer(r'File "([^"]+)", line (\d+)', output):
        _add(m.group(1), int(m.group(2)))
    for m in re.finditer(r'^(\S[^\n:]+\.py):(\d+): in ', output, re.MULTILINE):
        _add(m.group(1), int(m.group(2)))

    return frames


def _dedupe_frames(frames: list[tuple[str, int]], workspace: str) -> list[str]:
    """Return unique file paths from traceback frames: source files first, test files last."""
    seen: set[str] = set()
    source_files: list[str] = []
    test_files: list[str] = []

    for path, _ in frames:
        if path in seen:
            continue
        seen.add(path)
        parts = path.replace("\\", "/").split("/")
        if any("test" in p.lower() for p in parts):
            test_files.append(path)
        else:
            source_files.append(path)

    # One import-graph hop from source files; if no source files in traceback
    # (e.g. simple assertion failures), hop from test files to find source context.
    hop_seeds = source_files[:6] if source_files else test_files[:3]
    imported = _build_import_subgraph(workspace, hop_seeds, hops=1)
    imported_source = [f for f in imported if not any("test" in p.lower() for p in f.replace("\\", "/").split("/"))]
    all_files = source_files + [f for f in imported_source if f not in seen]
    all_files += test_files

    return all_files[:12]


# ── Internal: import graph ────────────────────────────────────────────────────


def _build_import_subgraph(workspace: str, seed_files: list[str], hops: int = 1) -> list[str]:
    """Walk import statements from seed_files one hop, returning new workspace .py files."""
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


def _build_full_import_graph(workspace: str, all_files: list[str]) -> dict[str, dict]:
    """Build a full import graph for all_files, returning per-file in-degree counts."""
    imported_by: dict[str, int] = {}

    for filepath in all_files:
        try:
            source = open(filepath, encoding="utf-8", errors="ignore").read()
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for mod in modules:
                cand = _module_to_file(workspace, mod)
                if cand:
                    imported_by[cand] = imported_by.get(cand, 0) + 1

    return {fp: {"imported_by_count": imported_by.get(fp, 0)} for fp in all_files}


def _module_to_file(workspace: str, module: str) -> str | None:
    direct = os.path.join(workspace, module.replace(".", os.sep) + ".py")
    if os.path.isfile(direct):
        return direct
    pkg = os.path.join(workspace, module.replace(".", os.sep), "__init__.py")
    if os.path.isfile(pkg):
        return pkg
    return None


# ── Internal: file loading ────────────────────────────────────────────────────


def _load_context_files(workspace: str, file_paths: list[str]) -> list[dict]:
    result: list[dict] = []
    for filepath in file_paths:
        if not os.path.isfile(filepath):
            continue
        content = _read_file_lines(filepath, max_lines=200)
        if content:
            result.append({
                "path": os.path.relpath(filepath, workspace),
                "content": content,
                "why": "appears in error traceback",
            })
    return result


def _read_file_lines(path: str, max_lines: int) -> str:
    try:
        lines: list[str] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f):
                if idx >= max_lines:
                    lines.append(f"... ({idx} more lines)\n")
                    break
                lines.append(line)
        return "".join(lines)
    except OSError:
        return ""


def _find_python_files(workspace: str) -> list[str]:
    result: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                result.append(os.path.join(root, f))
    return result


# ── Internal: repo metadata ───────────────────────────────────────────────────


def _load_repo_rules(workspace: str) -> list[str]:
    rules: list[str] = []
    for fname in _RULE_FILES:
        path = os.path.join(workspace, fname)
        if os.path.exists(path):
            try:
                content = open(path, encoding="utf-8").read(8000)
                rules.append(f"[from {fname}]\n{content}")
            except OSError:
                pass
    return rules


def _detect_build_commands(workspace: str) -> list[str]:
    cmds: list[str] = []

    if os.path.exists(os.path.join(workspace, "pyproject.toml")):
        try:
            content = open(os.path.join(workspace, "pyproject.toml")).read()
            cmds.append("python -m pytest" if "pytest" in content else "python -m pytest .")
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
            scripts = json.loads(open(pj).read()).get("scripts", {})
            if "test" in scripts:
                cmds.append(f"npm test")
        except Exception:
            cmds.append("npm test")

    if os.path.exists(os.path.join(workspace, "go.mod")):
        cmds.append("go test ./...")

    if not cmds:
        tests_dir = os.path.join(workspace, "tests")
        scope = "tests/" if os.path.isdir(tests_dir) else "."
        cmds.append(f"python3 -m pytest {scope} -x -q")

    return cmds


def _generate_repo_map(workspace: str) -> str:
    try:
        from tools.search import get_repo_map_tool
        return get_repo_map_tool({}, workspace)
    except Exception:
        return "(repo map unavailable)"

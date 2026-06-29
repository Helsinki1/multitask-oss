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


def _resolve_django_style(test_id: str, workspace: str) -> str:
    """Resolve a Django-format test ID: 'test_foo (module.path.ClassName)'."""
    m = re.match(r"^(\w+)\s+\(([^)]+)\)$", test_id.strip())
    if not m:
        return test_id
    func_name = m.group(1)
    dotted = m.group(2)
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


def resolve_test_id(workspace: str, test_id: str) -> str:
    """Convert a single test ID to a runnable pytest node ID (best-effort, no batch context)."""
    if _is_full_test_id(test_id):
        return test_id
    if re.match(r"^(\w+)\s+\(", test_id.strip()):
        return _resolve_django_style(test_id, workspace)
    # Bare name: global grep, take first hit
    try:
        r = subprocess.run(
            ["grep", "-r", f"def {test_id}", "--include=*.py", "-l", "."],
            capture_output=True, text=True, cwd=workspace, timeout=30,
        )
        hits = [l.strip() for l in r.stdout.splitlines()
                if "test" in l.lower() and l.strip().endswith(".py")]
        if hits:
            return f"{hits[0]}::{test_id}"
    except Exception:
        pass
    return test_id


def resolve_test_ids(workspace: str, test_ids: list[str]) -> list[str]:
    """Resolve a batch of test IDs to runnable pytest node IDs.

    For each bare function name that matches multiple test files, we break the
    tie using cross-reference: prefer the file that also defines the most OTHER
    test IDs in this batch.  This handles cases where tests come from more than
    one file — each test is resolved independently using the batch as context,
    rather than forcing a single global "best file" onto everything.
    """
    if not test_ids:
        return test_ids

    # Split into already-resolved and bare IDs (Django-style handled separately)
    bare_unique: list[str] = []
    seen: set[str] = set()
    for tid in test_ids:
        if not _is_full_test_id(tid) and not re.match(r"^(\w+)\s+\(", tid.strip()):
            if tid not in seen:
                bare_unique.append(tid)
                seen.add(tid)

    # Collect candidate files for every unique bare ID in one grep pass each
    candidates: dict[str, list[str]] = {}  # test_id → [file, ...]
    for tid in bare_unique:
        try:
            r = subprocess.run(
                ["grep", "-r", f"def {tid}", "--include=*.py", "-l", "."],
                capture_output=True, text=True, cwd=workspace, timeout=15,
            )
            hits = [l.strip() for l in r.stdout.splitlines()
                    if "test" in l.lower() and l.strip().endswith(".py")]
            candidates[tid] = hits
        except Exception:
            candidates[tid] = []

    # Score every file by how many distinct test IDs it covers across the batch
    from collections import Counter
    file_coverage: Counter = Counter()
    for tid, files in candidates.items():
        for f in files:
            file_coverage[f] += 1

    # Resolve: for each bare ID, pick the candidate file with highest batch coverage
    resolved_map: dict[str, str] = {}
    for tid in bare_unique:
        files = candidates.get(tid, [])
        if not files:
            resolved_map[tid] = tid  # fallback: leave unchanged
        elif len(files) == 1:
            resolved_map[tid] = f"{files[0]}::{tid}"
        else:
            best = max(files, key=lambda f: file_coverage[f])
            resolved_map[tid] = f"{best}::{tid}"

    # Apply resolutions, delegating Django-style and already-resolved IDs inline
    result: list[str] = []
    for tid in test_ids:
        if _is_full_test_id(tid):
            result.append(tid)
        elif re.match(r"^(\w+)\s+\(", tid.strip()):
            result.append(_resolve_django_style(tid, workspace))
        else:
            result.append(resolved_map.get(tid, tid))
    return result


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

    todo_list, tb_frames = _run_and_collect(workspace, fail_to_pass, pass_to_pass)
    # Extract just the file paths from f2p test IDs so test-import traversal can
    # always reach the source files, even when the traceback shows an ImportError.
    f2p_test_files = list({
        os.path.join(workspace, tid.split("::")[0])
        for tid in fail_to_pass
    })
    cb.task_adjacent_files = _load_context_files(
        workspace, tb_frames, f2p_test_files=f2p_test_files
    )

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
    cb.task_adjacent_files = _load_context_files(workspace, frames)

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
) -> tuple[TestToDoList, list[tuple[str, int]]]:
    """Run all tests, build initial TestToDoList, return (file, anchor_line) pairs."""
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
    return TestToDoList(cases=cases), frames


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


def _is_test_path(path: str) -> bool:
    return any("test" in p.lower() for p in path.replace("\\", "/").split("/"))


# ── Internal: import graph ────────────────────────────────────────────────────


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


def _module_to_file(workspace: str, module: str, relative_to: str = "") -> str | None:
    """Resolve a dotted module name to an on-disk .py file.

    relative_to: if non-empty, the directory of the file containing the import
    (used to resolve relative imports like '.matexpr').
    """
    if not module:
        return None
    base = relative_to if relative_to else workspace
    direct = os.path.join(base, module.replace(".", os.sep) + ".py")
    if os.path.isfile(direct):
        return direct
    pkg = os.path.join(base, module.replace(".", os.sep), "__init__.py")
    if os.path.isfile(pkg):
        return pkg
    # Fallback: try from workspace root if relative resolution failed
    if relative_to:
        return _module_to_file(workspace, module)
    return None


# ── Internal: file loading ────────────────────────────────────────────────────


def _parse_imports(path: str) -> dict[str, str]:
    """Return {imported_name: dotted_module} for every import in path.

    Handles both `from pkg import name` and `import pkg.mod [as alias]`.
    """
    try:
        source = open(path, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return {}

    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname if alias.asname else alias.name
                result[local] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname if alias.asname else alias.name
                result[local] = alias.name
    return result


def _find_cross_file_callees(
    path: str,
    func_starts: list[int],
    workspace: str,
) -> dict[str, str]:
    """Return {called_name: resolved_file_path} for imported names called by frame functions.

    Traces import statements in path, then walks each frame function body to find
    calls to imported names and resolves the module to an on-disk .py file.
    """
    imports = _parse_imports(path)
    if not imports:
        return {}

    try:
        source = open(path, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return {}

    func_start_set = set(func_starts)
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno not in func_start_set:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    called.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    called.add(child.func.attr)

    result: dict[str, str] = {}
    for name in called:
        if name not in imports:
            continue
        module = imports[name]
        resolved = _module_to_file(workspace, module)
        if resolved and os.path.isfile(resolved):
            result[name] = resolved
    return result


def _find_class_base_files(
    path: str,
    workspace: str,
    outline: list[dict],
) -> dict[str, list[tuple[str, str]]]:
    """Return {class_name: [(base_name, base_file_path)]} for classes defined in path.

    Only includes base classes that resolve to a .py file in the workspace.
    """
    imports = _parse_imports(path)
    class_names = {e["name"] for e in outline if "." not in e["name"]}

    try:
        source = open(path, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return {}

    result: dict[str, list[tuple[str, str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in class_names:
            continue
        bases: list[tuple[str, str]] = []
        for base in node.bases:
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if not base_name or base_name not in imports:
                continue
            resolved = _module_to_file(workspace, imports[base_name])
            if resolved and os.path.isfile(resolved):
                bases.append((base_name, resolved))
        if bases:
            result[node.name] = bases
    return result


def _find_local_callees(
    path: str,
    func_starts: list[int],
    outline: list[dict],
) -> set[str]:
    """Return base names of outline-defined functions called within the given functions.

    func_starts: 1-indexed start lines of call-site functions (traceback frames).
    Only returns names that are defined locally in this file (present in outline)
    and are not the frame functions themselves.
    """
    try:
        source = open(path, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return set()

    local_names = {e["name"].split(".")[-1] for e in outline}
    func_start_set = set(func_starts)
    frame_names: set[str] = set()
    called: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno not in func_start_set:
            continue
        frame_names.add(node.name)
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    called.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    called.add(child.func.attr)

    return (called & local_names) - frame_names


def _render_outline_map(
    outline: list[dict],
    frame_lines: set[int],
    callee_names: set[str],
    rel_path: str,
    class_bases: dict[str, list[tuple[str, str]]] | None = None,
    cross_file_callees: dict[str, str] | None = None,
    workspace: str = "",
) -> str:
    """Render a navigation map: all symbols with line ranges, traceback frames and callees marked.

    Also shows inheritance chains and cross-file callees so the agent knows where
    to read_file() next without grepping or reading from line 1.
    """
    if not outline:
        return ""

    lines = [
        f"NAVIGATION MAP — {rel_path} ({len(outline)} symbols)",
        f'  To read any symbol: read_file("{rel_path}", start_line=X, end_line=Y)',
    ]

    for entry in outline:
        name = entry["name"]
        lo = entry["lineno"]
        hi = entry["end_lineno"]
        base = name.split(".")[-1]
        is_method = "." in name
        indent = "    " if is_method else "  "

        is_frame = any(lo <= fl <= hi for fl in frame_lines)
        is_callee = base in callee_names and not is_frame

        has_children = not is_method and any(e["name"].startswith(name + ".") for e in outline)
        if has_children:
            label = f"class {name}"
        else:
            label = base if is_method else name

        doc = entry.get("docstring", "")
        doc_suffix = f" — {doc}" if doc else ""

        # Add inheritance annotation for class entries
        inherit_suffix = ""
        if has_children and class_bases and name in class_bases:
            parts = []
            for base_name, base_file in class_bases[name]:
                rel = os.path.relpath(base_file, workspace) if workspace else base_file
                parts.append(f"{base_name} → {rel}")
            inherit_suffix = f" (inherits: {', '.join(parts)})"

        if is_frame:
            marker = "  ⬅ TRACEBACK FRAME (shown below)"
        elif is_callee:
            marker = "  ← callee of traceback frame — read_file to see implementation"
        else:
            marker = ""

        lines.append(f"{indent}{label} [lines {lo}–{hi}]{inherit_suffix}{doc_suffix}{marker}")

    # Cross-file callees section
    if cross_file_callees:
        lines.append("  Cross-file callees (imported by traceback frames — see secondary context below):")
        for cname, cfile in cross_file_callees.items():
            rel = os.path.relpath(cfile, workspace) if workspace else cfile
            lines.append(f"    {cname} → {rel}")

    return "\n".join(lines)


def _load_context_files(
    workspace: str,
    frames: list[tuple[str, int]],
    max_lines_per_file: int = 450,
    head_lines: int = 60,
    f2p_test_files: list[str] | None = None,
) -> list[dict]:
    """Load context files: navigation map + header + all call-site functions.

    For each traceback file:
      - NAVIGATION MAP lists every symbol with its exact line range, marking:
          ⬅ TRACEBACK FRAME — already shown in full below
          ← callee — called by frame function in same file
          (inherits: X → file.py) — class base chain, file included as secondary context
          Cross-file callees section — imported names called by frame functions
      - File content shows the first head_lines (imports/module setup) plus the full
        AST-bounded function for every traceback anchor in that file.

    Secondary context (base classes + cross-file callees) is appended after primary
    files with outline-only maps so the agent can navigate without reading from line 1.
    Source files come before test files; primary capped at 10, secondary at 4.

    f2p_test_files: if provided, test-import traversal always runs for these files
    (regardless of whether they appear in the traceback). This ensures the agent sees
    the implementation files even when the test fails with an ImportError.
    """
    file_anchors: dict[str, list[int]] = {}
    for path, lineno in frames:
        if not os.path.isfile(path):
            continue
        if path not in file_anchors:
            file_anchors[path] = []
        file_anchors[path].append(lineno)

    source_files = [p for p in file_anchors if not _is_test_path(p)]
    test_files = [p for p in file_anchors if _is_test_path(p)]
    ordered = (source_files + test_files)[:10]

    # Collect secondary files (base classes + cross-file callees) during primary pass
    secondary: dict[str, str] = {}       # file_path → reason string
    secondary_anchors: dict[str, list[int]] = {}  # file_path → list of anchor line numbers

    result: list[dict] = []
    for filepath in ordered:
        anchors = file_anchors[filepath]
        rel = os.path.relpath(filepath, workspace)

        outline = extract_file_outline(filepath)
        frame_lines = set(anchors)

        # Map each anchor to the start line of its containing function (for AST lookup)
        func_starts = [
            _find_containing_function(filepath, a)[0]
            for a in anchors if a > 0
        ]
        callee_names = _find_local_callees(filepath, func_starts, outline)

        # Resolve cross-file callees (imported names called by frame functions)
        cross_file = _find_cross_file_callees(filepath, func_starts, workspace)
        # Exclude files already in primary context
        cross_file = {k: v for k, v in cross_file.items() if v not in file_anchors}
        # Group callees by target file so we can record all relevant anchors per file
        cross_file_by_dep: dict[str, list[str]] = {}
        for name, dep_file in cross_file.items():
            cross_file_by_dep.setdefault(dep_file, []).append(name)
        for dep_file, names in cross_file_by_dep.items():
            if dep_file not in secondary:
                secondary[dep_file] = (
                    f"cross-file callees {names!r} called by traceback frame in {rel}"
                )
            # Record anchors so secondary rendering shows the actual function bodies
            if dep_file not in secondary_anchors:
                secondary_anchors[dep_file] = []
            dep_outline = extract_file_outline(dep_file)
            for oentry in dep_outline:
                top = oentry["name"].split(".")[0]
                if top in names:
                    secondary_anchors[dep_file].append(oentry["lineno"])

        # Resolve class base files
        class_bases = _find_class_base_files(filepath, workspace, outline)
        for class_name, bases in class_bases.items():
            for base_name, base_file in bases:
                if base_file not in file_anchors and base_file not in secondary:
                    secondary[base_file] = f"base class '{base_name}' of class '{class_name}' defined in {rel}"

        nav_map = _render_outline_map(
            outline, frame_lines, callee_names, rel,
            class_bases=class_bases,
            cross_file_callees={k: v for k, v in cross_file.items()},
            workspace=workspace,
        )
        file_content = _read_file_tiered(filepath, anchors, max_lines_per_file, head_lines)

        full = "\n\n".join(part for part in [nav_map, file_content] if part)
        if full:
            result.append({
                "path": rel,
                "content": full,
                "why": "appears in error traceback",
            })

    # Follow named imports from f2p test files to find source files being exercised.
    # Always runs for f2p_test_files (even when the traceback has non-test frames, e.g.
    # an ImportError that hides the actual assertion failure). Also runs when the primary
    # context consists only of test files (shallow traceback / assertion failure).
    # For each found secondary file, we record line-number anchors for the imported names
    # so the secondary context shows actual function bodies, not just outlines.

    def _register_secondary_with_anchors(rfile: str, reason: str, names: list[str]) -> None:
        """Add rfile to secondary context; record outline anchors for the listed names."""
        if rfile in file_anchors:
            return
        if rfile not in secondary:
            secondary[rfile] = reason
        if rfile not in secondary_anchors:
            secondary_anchors[rfile] = []
        rfile_outline = extract_file_outline(rfile)
        for oentry in rfile_outline:
            top = oentry["name"].split(".")[0]
            if top in names:
                secondary_anchors[rfile].append(oentry["lineno"])

    primary_is_test_only = all(_is_test_path(p) for p in ordered if p)
    # Collect test files to traverse: from traceback (when all primary are tests)
    # plus any f2p_test_files provided explicitly by the caller.
    test_files_to_traverse: list[str] = []
    if primary_is_test_only:
        test_files_to_traverse = list(ordered[:3])
    if f2p_test_files:
        for tf in f2p_test_files:
            abs_tf = tf if os.path.isabs(tf) else os.path.join(workspace, tf)
            if os.path.isfile(abs_tf) and abs_tf not in test_files_to_traverse:
                test_files_to_traverse.append(abs_tf)

    if test_files_to_traverse:
        for filepath in test_files_to_traverse[:3]:
            try:
                source = open(filepath, encoding="utf-8", errors="ignore").read()
                tree = ast.parse(source)
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                # Only follow specific named imports, not star imports
                if any(a.name == "*" for a in node.names):
                    continue
                imported_names = [a.name for a in node.names if a.name != "*"]
                resolved = _module_to_file(workspace, node.module)
                if not resolved or not os.path.isfile(resolved):
                    continue
                if _is_test_path(resolved):
                    continue
                if resolved in file_anchors:
                    continue
                rel_test = os.path.relpath(filepath, workspace)

                _register_secondary_with_anchors(
                    resolved,
                    f"imported by test file {rel_test} — likely contains the implementation to fix",
                    imported_names,
                )

                # If resolved is an __init__.py, follow its specific imports one more hop
                # (__init__.py files are usually re-exports; the real code is one level deeper)
                if resolved.endswith("__init__.py"):
                    init_dir = os.path.dirname(resolved)
                    try:
                        init_src = open(resolved, encoding="utf-8", errors="ignore").read()
                        init_tree = ast.parse(init_src)
                    except (SyntaxError, OSError):
                        continue
                    for inode in ast.walk(init_tree):
                        if not isinstance(inode, ast.ImportFrom):
                            continue
                        if any(a.name == "*" for a in inode.names):
                            continue
                        imod = inode.module or ""
                        ilevel = inode.level  # relative import depth (0 = absolute)
                        if ilevel > 0:
                            # Relative import: resolve from init_dir going up (ilevel-1) dirs
                            base_dir = init_dir
                            for _ in range(ilevel - 1):
                                base_dir = os.path.dirname(base_dir)
                            iresolve = _module_to_file(workspace, imod, relative_to=base_dir) if imod else None
                            if not iresolve:
                                # bare relative: e.g. `from . import matexpr`
                                for alias in inode.names:
                                    if alias.name == "*":
                                        continue
                                    candidate = os.path.join(base_dir, alias.name + ".py")
                                    if os.path.isfile(candidate):
                                        iresolve = candidate
                                        break
                        else:
                            iresolve = _module_to_file(workspace, imod) if imod else None
                        if iresolve and os.path.isfile(iresolve) and not _is_test_path(iresolve):
                            inames = [a.name for a in inode.names if a.name != "*"]
                            _register_secondary_with_anchors(
                                iresolve,
                                f"re-exported via {os.path.relpath(resolved, workspace)}"
                                f" (imported by test file {rel_test})",
                                inames,
                            )

    # Append secondary files — outline + content.
    # For files reached via test imports: full tiered read of the imported symbols
    #   (agent sees actual code of the imported classes/functions, not just the outline).
    # For other secondary files (base classes, cross-file callees): outline + 40-line header.
    seen_secondary = 0
    for dep_file, reason in secondary.items():
        if seen_secondary >= 5:
            break
        if not os.path.isfile(dep_file):
            continue
        dep_outline = extract_file_outline(dep_file)
        dep_rel = os.path.relpath(dep_file, workspace)
        anchors_for_dep = secondary_anchors.get(dep_file, [])
        dep_map = _render_outline_map(dep_outline, set(anchors_for_dep), set(), dep_rel)

        if anchors_for_dep:
            # Show actual code of the imported classes/functions
            dep_content = _read_file_tiered(dep_file, anchors_for_dep, max_lines=350, head_lines=40)
        else:
            dep_content = _read_file_lines(dep_file, max_lines=40)
            if dep_content:
                dep_content = (
                    "[first 40 lines — use read_file(start_line=X, end_line=Y) for any function body]\n"
                    + dep_content
                )

        sections = [f"SECONDARY CONTEXT — included because: {reason}"]
        if dep_map:
            sections.append(dep_map)
        if dep_content:
            sections.append(dep_content)
        if len(sections) > 1:
            result.append({
                "path": dep_rel,
                "content": "\n\n".join(sections),
                "why": reason,
            })
            seen_secondary += 1

    return result


def _render_with_gaps(all_lines: list[str], included: list[int], total: int) -> str:
    """Render a sorted list of 0-indexed line numbers with gap annotations."""
    if not included:
        return ""
    parts: list[str] = []
    prev = -2
    for i in included:
        if i != prev + 1:
            if prev >= 0:
                parts.append(
                    f"     ... ({i - prev - 1} lines skipped"
                    f" — use read_file to inspect) ...\n"
                )
        parts.append(f"{i + 1:4d} | {all_lines[i]}")
        prev = i
    tail = total - 1 - included[-1]
    if tail > 0:
        parts.append(f"     ... ({tail} more lines — use read_file to inspect) ...\n")
    return "".join(parts)


def _read_file_tiered(
    path: str,
    anchors: list[int],
    max_lines: int = 450,
    head_lines: int = 60,
) -> str:
    """Read file header + full containing function for every call-site anchor.

    Rule: always include the first head_lines (captures imports / module-level code)
    plus the complete AST-bounded function for each anchor line in the traceback.
    Total line budget is capped at max_lines; if exceeded, functions are added in
    traceback order and the remainder filled with lines closest to each anchor.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
    except OSError:
        return ""
    total = len(all_lines)
    if total == 0:
        return ""

    # Build include set: header always, then each call-site function
    include: set[int] = set(range(min(head_lines, total)))
    for anchor in sorted(set(anchors)):
        if anchor <= 0 or anchor > total:
            continue
        fs, fe = _find_containing_function(path, anchor)
        include.update(range(fs - 1, min(fe, total)))

    if len(include) <= max_lines:
        return _render_with_gaps(all_lines, sorted(include), total)

    # Over budget: keep header, then greedily add each function in anchor order
    trimmed: set[int] = set(range(min(head_lines, max_lines, total)))
    for anchor in sorted(set(anchors)):
        if len(trimmed) >= max_lines or anchor <= 0 or anchor > total:
            continue
        fs, fe = _find_containing_function(path, anchor)
        func_lines = set(range(fs - 1, min(fe, total)))
        new_lines = func_lines - trimmed
        if len(trimmed) + len(new_lines) <= max_lines:
            trimmed.update(func_lines)
        else:
            # Fill remaining budget prioritising lines closest to the anchor
            budget = max_lines - len(trimmed)
            anchor_0 = anchor - 1
            by_proximity = sorted(new_lines, key=lambda x: abs(x - anchor_0))
            trimmed.update(by_proximity[:budget])

    return _render_with_gaps(all_lines, sorted(trimmed), total)


def _read_file_lines(path: str, max_lines: int) -> str:
    """Legacy: read first max_lines lines with line numbers. Used for prior-file refresh."""
    try:
        lines: list[str] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        for idx, line in enumerate(all_lines[:max_lines]):
            lines.append(f"{idx + 1:4d} | {line}")
        if len(all_lines) > max_lines:
            lines.append(f"... ({len(all_lines) - max_lines} more lines — use read_file to inspect)\n")
        return "".join(lines)
    except OSError:
        return ""


def _find_containing_function(path: str, anchor: int) -> tuple[int, int]:
    """Return 1-indexed (start, end) of the innermost function/class containing anchor.

    Falls back to an ±80-line window if AST parse fails or no function wraps anchor.
    """
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            source = f.read()
        total = source.count("\n") + 1
    except OSError:
        return (max(1, anchor - 80), anchor + 80)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return (max(1, anchor - 80), min(anchor + 80, total))

    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not hasattr(node, "end_lineno"):
            continue
        if node.lineno <= anchor <= node.end_lineno:
            span = node.end_lineno - node.lineno
            if best is None or span < (best[1] - best[0]):
                best = (node.lineno, node.end_lineno)

    if best is None:
        return (max(1, anchor - 80), min(anchor + 80, total))
    return best




def _first_docstring_line(node: ast.AST) -> str:
    body = getattr(node, "body", [])
    if (body and isinstance(body[0], ast.Expr) and
            isinstance(body[0].value, ast.Constant) and
            isinstance(body[0].value.value, str)):
        return body[0].value.value.strip().splitlines()[0][:100]
    return ""


def extract_file_outline(path: str) -> list[dict]:
    """Return all top-level functions and class methods with line ranges and docstrings.

    Used by gather_context.py to build LLM ranking prompts.
    """
    try:
        source = open(path, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return []

    result: list[dict] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append({
                "name": node.name,
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
                "docstring": _first_docstring_line(node),
            })
        elif isinstance(node, ast.ClassDef):
            result.append({
                "name": node.name,
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
                "docstring": _first_docstring_line(node),
            })
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result.append({
                        "name": f"{node.name}.{child.name}",
                        "lineno": child.lineno,
                        "end_lineno": getattr(child, "end_lineno", child.lineno),
                        "docstring": _first_docstring_line(child),
                    })

    return sorted(result, key=lambda x: x["lineno"])


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



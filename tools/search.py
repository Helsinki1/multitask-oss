"""Search and directory listing tools."""

import os
import subprocess


def list_files_tool(args: dict, workspace: str) -> str:
    subpath = args.get("path", "")
    max_depth = min(int(args.get("max_depth", 3)), 6)

    root = os.path.realpath(os.path.join(workspace, subpath) if subpath else workspace)
    if not os.path.isdir(root):
        return f"Error: directory not found: {subpath or '.'}"

    lines: list[str] = []
    _SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", "dist", "build"}

    for dirpath, dirnames, filenames in os.walk(root):
        # Compute depth relative to root
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames.clear()
            continue

        # Prune skipped dirs in-place
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP)

        indent = "  " * depth
        folder_name = os.path.basename(dirpath) if depth > 0 else "."
        lines.append(f"{indent}{folder_name}/")

        for fname in sorted(filenames):
            size = 0
            try:
                size = os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
            size_str = f" ({size:,}b)" if size < 10_000 else f" ({size // 1024}KB)"
            lines.append(f"{indent}  {fname}{size_str}")

    return "\n".join(lines) if lines else "(empty)"


def search_repo_tool(args: dict, workspace: str) -> str:
    query = args["query"]
    subpath = args.get("path", "")
    file_glob = args.get("file_glob", "")

    search_root = os.path.join(workspace, subpath) if subpath else workspace

    # Try rg (ripgrep) first, fall back to grep
    try:
        cmd = ["rg", "--line-number", "--color=never", "--max-count=50"]
        if file_glob:
            cmd += ["--glob", file_glob]
        cmd += [query, search_root]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout
    except FileNotFoundError:
        # ripgrep not available — use grep
        cmd = ["grep", "-rn", "--include=" + (file_glob or "*"), query, search_root]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout
        except Exception as exc:
            return f"Error running search: {exc}"

    if not output.strip():
        return f"No matches for: {query}"

    lines = output.splitlines()
    if len(lines) > 50:
        lines = lines[:50]
        lines.append(f"... (showing first 50 matches)")

    # Make paths relative to workspace
    result_lines = []
    for line in lines:
        if line.startswith(workspace):
            line = line[len(workspace):].lstrip("/")
        result_lines.append(line)

    return "\n".join(result_lines)


def get_repo_map_tool(args: dict, workspace: str) -> str:
    """Generate a lightweight structural overview of the repo."""
    lines: list[str] = []

    # 1. Directory tree (depth 2)
    lines.append("=== Repository Structure ===")
    lines.append(list_files_tool({"max_depth": 2}, workspace))
    lines.append("")

    # 2. Key config/manifest files
    manifests = [
        "pyproject.toml", "setup.py", "setup.cfg",
        "package.json", "go.mod", "Cargo.toml", "Makefile",
        "AGENTS.md", "CLAUDE.md", ".cursor/rules",
    ]
    found_manifests: list[str] = []
    for mf in manifests:
        p = os.path.join(workspace, mf)
        if os.path.exists(p):
            found_manifests.append(mf)
    if found_manifests:
        lines.append("=== Config/Manifest Files ===")
        lines.append(", ".join(found_manifests))
        lines.append("")

    # 3. Python: list top-level functions/classes per .py file
    py_files = _find_py_files(workspace, max_files=30)
    if py_files:
        lines.append("=== Python Symbols ===")
        for pyf in py_files:
            rel = os.path.relpath(pyf, workspace)
            symbols = _extract_py_symbols(pyf)
            if symbols:
                lines.append(f"{rel}: {', '.join(symbols)}")
        lines.append("")

    return "\n".join(lines)


def _find_py_files(workspace: str, max_files: int = 30) -> list[str]:
    result: list[str] = []
    skip = {".git", "__pycache__", ".venv", "venv", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            if f.endswith(".py"):
                result.append(os.path.join(dirpath, f))
                if len(result) >= max_files:
                    return result
    return result


def _extract_py_symbols(path: str) -> list[str]:
    symbols: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(("def ", "class ", "async def ")):
                    name = stripped.split("(")[0].split()[-1]
                    symbols.append(name)
    except OSError:
        pass
    return symbols

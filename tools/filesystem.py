"""File read/write/patch tools. All paths are resolved relative to workspace."""

import os
import subprocess


def _resolve(path: str, workspace: str) -> str:
    """Resolve path relative to workspace, reject traversal attacks."""
    full = os.path.realpath(os.path.join(workspace, path))
    workspace_real = os.path.realpath(workspace)
    if not full.startswith(workspace_real + os.sep) and full != workspace_real:
        raise ValueError(f"Path traversal rejected: {path}")
    return full


def read_file_tool(args: dict, workspace: str) -> str:
    path = args["path"]
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    try:
        full = _resolve(path, workspace)
    except ValueError as e:
        return f"Error: {e}"

    if not os.path.exists(full):
        return f"Error: file not found: {path}"
    if os.path.isdir(full):
        return f"Error: {path} is a directory — use list_files"

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as exc:
        return f"Error reading {path}: {exc}"

    total = len(lines)
    offset = (start_line - 1) if start_line else 0
    if start_line:
        end_idx = end_line if end_line else total
        lines = lines[start_line - 1 : end_idx]
    else:
        lines = lines[:2000]

    truncated = total > 2000 and not start_line
    out = "".join(f"{offset + i + 1:4d} | {line}" for i, line in enumerate(lines))
    if truncated:
        out += f"\n... ({total} total lines, showing first 2000)"
    return out


def write_file_tool(args: dict, workspace: str) -> str:
    path = args["path"]
    content = args["content"]

    try:
        full = _resolve(path, workspace)
    except ValueError as e:
        return f"Error: {e}"

    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Written: {path} ({len(content)} bytes)"


def replace_in_file_tool(args: dict, workspace: str) -> str:
    path = args["path"]
    old_text = args["old_text"]
    new_text = args["new_text"]
    expected = args.get("expected_replacements", 1)

    try:
        full = _resolve(path, workspace)
    except ValueError as e:
        return f"Error: {e}"

    if not os.path.exists(full):
        return f"Error: file not found: {path}"

    with open(full, "r", encoding="utf-8") as f:
        content = f.read()

    count = content.count(old_text)
    if count == 0:
        # Show nearby context to help debug
        lines = content.splitlines()
        preview = "\n".join(lines[:20]) if lines else "(empty file)"
        return (
            f"Error: old_text not found in {path}.\n"
            f"First 20 lines of file:\n{preview}"
        )
    if expected and count != expected:
        return f"Error: found {count} occurrences, expected {expected}. Aborting for safety."

    new_content = content.replace(old_text, new_text)
    with open(full, "w", encoding="utf-8") as f:
        f.write(new_content)
    return f"Replaced {count} occurrence(s) in {path}"


def apply_patch_tool(args: dict, workspace: str) -> str:
    patch = args["patch"]

    # Try patch(1) first
    result = subprocess.run(
        ["patch", "-p1", "--batch", "--forward"],
        input=patch,
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    if result.returncode == 0:
        return f"Patch applied.\n{result.stdout.strip()}"
    return (
        f"Patch failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\n"
        f"stderr: {result.stderr[:500]}"
    )

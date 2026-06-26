"""Git tools: status, diff, log."""

import subprocess


def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return result.returncode, result.stdout, result.stderr


def git_status_tool(args: dict, workspace: str) -> str:
    rc, out, err = _git(["status", "--porcelain", "-b"], workspace)
    if rc != 0:
        return f"git status failed: {err}"
    if not out.strip():
        return "Working tree clean, no changes."
    return out


def git_diff_tool(args: dict, workspace: str) -> str:
    base = args.get("base", "HEAD")
    if base == "HEAD":
        rc, out, err = _git(["diff", "HEAD"], workspace)
    else:
        rc, out, err = _git(["diff", base], workspace)

    if rc != 0:
        return f"git diff failed: {err}"

    if not out.strip():
        # Try staged
        rc2, out2, _ = _git(["diff", "--staged"], workspace)
        if out2.strip():
            return "=== Staged changes ===\n" + out2[:8000]
        return "No diff (nothing changed from HEAD)"

    if len(out) > 8000:
        stat_rc, stat_out, _ = _git(["diff", "--stat", base], workspace)
        return f"=== Diff stat ===\n{stat_out}\n\n=== Diff (truncated at 8000 chars) ===\n{out[:8000]}"
    return out


def git_log_tool(args: dict, workspace: str) -> str:
    max_count = min(int(args.get("max_count", 10)), 50)
    rc, out, err = _git(
        ["log", f"--max-count={max_count}", "--oneline", "--graph"],
        workspace,
    )
    if rc != 0:
        return f"git log failed: {err}"
    return out or "No commits."

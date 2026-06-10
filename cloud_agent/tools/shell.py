"""Shell execution tool."""

import os
import subprocess


def run_shell_tool(args: dict, workspace: str) -> str:
    command = args["command"]
    rel_cwd = args.get("cwd", "")
    timeout = min(int(args.get("timeout_seconds", 120)), 600)

    cwd = os.path.join(workspace, rel_cwd) if rel_cwd else workspace
    if not os.path.isdir(cwd):
        return f"Error: working directory not found: {rel_cwd}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command}"
    except Exception as exc:
        return f"Error running command: {exc}"

    stdout = result.stdout
    stderr = result.stderr

    # Truncate large outputs
    MAX_OUT = 4096
    MAX_ERR = 2048
    stdout_trunc = ""
    stderr_trunc = ""
    if len(stdout) > MAX_OUT:
        stdout_trunc = f"\n... (truncated, {len(stdout)} total bytes)"
        stdout = stdout[:MAX_OUT]
    if len(stderr) > MAX_ERR:
        stderr_trunc = f"\n... (truncated, {len(stderr)} total bytes)"
        stderr = stderr[:MAX_ERR]

    parts = [f"$ {command}", f"exit code: {result.returncode}"]
    if stdout.strip():
        parts.append(f"stdout:\n{stdout}{stdout_trunc}")
    if stderr.strip():
        parts.append(f"stderr:\n{stderr}{stderr_trunc}")

    return "\n".join(parts)

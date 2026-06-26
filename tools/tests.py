"""Test runner tool with parsed output (pytest, go test, jest, etc.)."""

import re
import subprocess
import os


def run_tests_tool(args: dict, workspace: str) -> str:
    command = args["command"]
    rel_cwd = args.get("cwd", "")
    timeout = min(int(args.get("timeout_seconds", 300)), 600)

    cwd = os.path.join(workspace, rel_cwd) if rel_cwd else workspace

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
        return f"Tests timed out after {timeout}s: {command}"
    except Exception as exc:
        return f"Error running tests: {exc}"

    stdout = result.stdout
    stderr = result.stderr
    exit_code = result.returncode

    # Parse test counts from common frameworks
    summary = _parse_test_summary(stdout, stderr, command)

    MAX = 6000
    if len(stdout) > MAX:
        stdout = stdout[-MAX:]  # show tail — errors usually at end
        stdout = f"... (output truncated, showing last {MAX} chars)\n" + stdout

    parts = [
        f"$ {command}",
        f"exit code: {exit_code}",
        summary,
    ]
    if stdout.strip():
        parts.append(f"stdout:\n{stdout}")
    if stderr.strip() and len(stderr) < 2000:
        parts.append(f"stderr:\n{stderr}")

    return "\n".join(p for p in parts if p)


def _parse_test_summary(stdout: str, stderr: str, command: str) -> str:
    combined = stdout + "\n" + stderr

    # pytest: "5 passed, 2 failed, 1 error"
    m = re.search(
        r"(\d+) passed(?:,\s*(\d+) failed)?(?:,\s*(\d+) error)?(?:,\s*(\d+) warning)?",
        combined,
    )
    if m:
        passed = m.group(1)
        failed = m.group(2) or "0"
        errors = m.group(3) or "0"
        return f"pytest: {passed} passed, {failed} failed, {errors} error(s)"

    # go test: "ok" or "FAIL"
    if re.search(r"\bok\s+\S+", combined):
        return "go test: PASSED"
    if re.search(r"\bFAIL\b", combined):
        return "go test: FAILED"

    # jest: "Tests: N passed, N total"
    m = re.search(r"Tests:\s+(.+)", combined)
    if m:
        return f"jest: {m.group(1)}"

    return ""

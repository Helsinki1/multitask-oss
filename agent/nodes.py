"""
LangGraph node functions. Each receives AgentState and returns a partial update dict.
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent.docker_runner import run_tests as docker_run_tests
from agent.state import AgentState, log_event
from agent.tools import apply_patch as fs_apply_patch
from agent.tools import list_files as fs_list_files
from agent.tools import read_file

_llm = ChatOpenAI(model="gpt-4o", max_tokens=4096)

# ---------------------------------------------------------------------------
# Non-LLM nodes
# ---------------------------------------------------------------------------


def list_files_node(state: AgentState) -> dict:
    files = fs_list_files(state["repo_path"])
    log_event("list_files", files=files)
    return {"file_listing": files}


def apply_patch_node(state: AgentState) -> dict:
    patches = state["patches_attempted"]
    if not patches:
        return {"patch_errors": state.get("patch_errors", [])}

    latest = patches[-1]
    success, stderr = fs_apply_patch(state["repo_path"], latest)
    errors = list(state.get("patch_errors", []))
    errors.append(stderr if not success else "")
    log_event("apply_patch", iteration=state["iteration"], success=success, stderr=stderr)
    return {"patch_errors": errors}


def run_tests_node(state: AgentState) -> dict:
    result = docker_run_tests(state["repo_path"])
    runs = list(state.get("test_runs", []))
    runs.append(result)
    log_event(
        "run_tests",
        iteration=state["iteration"],
        exit_code=result["exit_code"],
        stdout=result["stdout"][:2000],
        stderr=result["stderr"][:500],
    )
    return {"test_runs": runs}


# ---------------------------------------------------------------------------
# LLM nodes
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a coding agent. You are given a task and information about a Python repository.
Respond concisely. Follow instructions exactly.
"""


def read_files_node(state: AgentState) -> dict:
    """Ask the LLM which files to read, then read them."""
    file_list = "\n".join(state["file_listing"])
    prompt = (
        f"Task: {state['task']}\n\n"
        f"Files in repo:\n{file_list}\n\n"
        "List the file paths you need to read to understand and fix the problem. "
        "Reply with one relative file path per line, nothing else."
    )
    response = _llm.invoke([SystemMessage(_SYSTEM), HumanMessage(prompt)])
    chosen = [line.strip() for line in response.content.strip().splitlines() if line.strip()]

    files_read: dict[str, str] = {}
    for rel in chosen:
        if rel in state["file_listing"]:
            try:
                files_read[rel] = read_file(state["repo_path"], rel)
            except Exception as exc:
                files_read[rel] = f"<error reading: {exc}>"
    log_event("read_files", files=list(files_read.keys()))
    return {"files_read": files_read}


def plan_node(state: AgentState) -> dict:
    """Generate a short plan from the task + file contents."""
    file_dump = "\n\n".join(
        f"### {path}\n```python\n{content}\n```"
        for path, content in state["files_read"].items()
    )
    prompt = (
        f"Task: {state['task']}\n\n"
        f"{file_dump}\n\n"
        "Write a short bulleted plan (≤5 bullets) describing exactly what code changes "
        "are needed to fix the problem. No prose, no explanations beyond the bullets."
    )
    response = _llm.invoke([SystemMessage(_SYSTEM), HumanMessage(prompt)])
    plan = response.content.strip()
    log_event("plan", plan=plan)
    return {"plan": plan}


def generate_patch_node(state: AgentState) -> dict:
    """Generate a unified diff patch to apply to the repo."""
    file_dump = "\n\n".join(
        f"### {path}\n```\n{content}\n```"
        for path, content in state["files_read"].items()
    )
    prev_errors = ""
    if state.get("patch_errors"):
        last_error = state["patch_errors"][-1]
        if last_error:
            prev_errors = f"\nPrevious patch failed with: {last_error}\n"

    prev_test = ""
    if state.get("test_runs"):
        last = state["test_runs"][-1]
        prev_test = (
            f"\nLast test run (exit code {last['exit_code']}):\n"
            f"stdout:\n{last['stdout'][:1500]}\n"
            f"stderr:\n{last['stderr'][:500]}\n"
        )

    prompt = (
        f"Task: {state['task']}\n\n"
        f"Plan:\n{state['plan']}\n\n"
        f"Current file contents:\n{file_dump}\n"
        f"{prev_errors}{prev_test}\n"
        "Produce a unified diff patch compatible with `patch -p1`. "
        "Use paths like 'a/filename.py' and 'b/filename.py'. "
        "Output ONLY the raw patch text (--- / +++ / @@ lines), no markdown fences, no explanation."
    )
    response = _llm.invoke([SystemMessage(_SYSTEM), HumanMessage(prompt)])
    patch = response.content.strip()
    # Strip accidental markdown fences
    if patch.startswith("```"):
        lines = patch.splitlines()
        patch = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    patches = list(state.get("patches_attempted", []))
    patches.append(patch)
    log_event("generate_patch", iteration=state["iteration"], patch=patch)
    return {"patches_attempted": patches}


def analyze_node(state: AgentState) -> dict:
    """Decide if tests passed; increment iteration counter."""
    last_run = state["test_runs"][-1] if state["test_runs"] else {}
    exit_code = last_run.get("exit_code", 1)

    if exit_code == 0:
        log_event("analyze", iteration=state["iteration"], verdict="passed")
        return {"test_passed": True, "iteration": state["iteration"] + 1}

    # Re-read files so the next generate_patch sees the current state
    files_read: dict[str, str] = {}
    for rel in state["files_read"]:
        if rel in state["file_listing"]:
            try:
                files_read[rel] = read_file(state["repo_path"], rel)
            except Exception:
                files_read[rel] = state["files_read"][rel]

    new_iter = state["iteration"] + 1
    log_event("analyze", iteration=state["iteration"], verdict="failing", next_iteration=new_iter)
    return {"test_passed": False, "iteration": new_iter, "files_read": files_read}


def summarize_node(state: AgentState) -> dict:
    """Produce a human-readable final summary."""
    status = "PASSED" if state.get("test_passed") else "FAILED (iteration limit reached)"
    patches_count = len(state.get("patches_attempted", []))
    runs_count = len(state.get("test_runs", []))
    last_output = ""
    if state.get("test_runs"):
        last = state["test_runs"][-1]
        last_output = last.get("stdout", "")[:1000]

    summary = (
        f"=== Agent Summary ===\n"
        f"Task: {state['task']}\n"
        f"Result: {status}\n"
        f"Iterations: {state['iteration']}\n"
        f"Patches attempted: {patches_count}\n"
        f"Test runs: {runs_count}\n"
        f"\n--- Final test output ---\n{last_output}"
    )
    log_event("summarize", status=status, iterations=state["iteration"])
    return {"summary": summary, "done": True}

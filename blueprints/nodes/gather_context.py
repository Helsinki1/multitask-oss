"""Node GATHER_CONTEXT: deterministic traceback-driven context gathering.

Runs the failing tests, parses tracebacks, follows the import graph one hop,
and loads the causally-adjacent source files as pre-loaded context for IMPLEMENT.

Also handles the p2p-regression re-entry path: when VERIFY detects pass_to_pass
failures it routes here to re-derive context from the regression tracebacks
already stored in the TestToDoList.
"""

from __future__ import annotations
import os

from agent.context import (
    _read_file_smart,
    build_bugfix_context,
    extract_file_outline,
    rebuild_context_from_regressions,
)
from agent.runtime import Node, NodeResult
from agent.state import AgentState, ContextBundle, TestToDoList
from observability.tracer import Tracer


def _rank_sections(
    cb: ContextBundle,
    task_text: str,
    todo_list: TestToDoList,
    workspace: str,
    model: str,
) -> list[dict]:
    """Call the discovery model to rank additional file sections by relevance.

    Returns list of {"path": str, "sections": ["FuncName (lines X-Y): reason", ...]}.
    Only processes non-test source files with enough defined functions to be worth ranking.
    """
    import openai
    from cloud_agent.config import settings

    client = openai.OpenAI(api_key=settings.openai_api_key)

    tb_text = "\n".join(
        c.traceback for c in todo_list.cases
        if c.category == "fail_to_pass" and c.traceback
    )[:1500]

    ranked: list[dict] = []
    for file_info in cb.task_adjacent_files:
        path = file_info.get("path", "")
        path_parts = path.replace("\\", "/").split("/")
        if any("test" in p.lower() for p in path_parts):
            continue

        abs_path = os.path.join(workspace, path)
        if not os.path.isfile(abs_path):
            continue

        outline = extract_file_outline(abs_path)
        if len(outline) < 3:
            continue

        outline_text = "\n".join(
            f"  {fn['name']} (lines {fn['lineno']}-{fn['end_lineno']})"
            + (f": {fn['docstring']}" if fn["docstring"] else "")
            for fn in outline[:40]
        )

        prompt = (
            f"File: {path}\n"
            f"Task: {task_text[:200]}\n\n"
            f"Traceback excerpt:\n{tb_text[:600]}\n\n"
            f"Functions/classes in this file:\n{outline_text}\n\n"
            "List up to 4 functions most relevant to investigate for this bug "
            "(beyond the error site itself). Reply one per line:\n"
            "FunctionName (lines X-Y): one-sentence reason\n"
            "If none are clearly relevant, reply: none"
        )

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_completion_tokens=200,
            )
            raw = resp.choices[0].message.content.strip()
            if raw.lower().startswith("none"):
                continue
            sections: list[str] = []
            for line in raw.splitlines():
                line = line.strip().lstrip("•*-0123456789.)").strip()
                if line and not line.lower().startswith("none"):
                    sections.append(line)
            if sections:
                ranked.append({"path": path, "sections": sections})
        except Exception:
            continue

    return ranked


class GatherContextNode(Node):
    name = "GATHER_CONTEXT"
    node_type = "deterministic"
    failure_next = "CHECKPOINT"

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def run(self, state: AgentState) -> NodeResult:
        ws = state.workspace_path

        if state.verify_failure_type == "p2p_regression":
            # Re-entry from VERIFY after regression: re-derive context from p2p tracebacks
            self.tracer.emit("gather_context.p2p_retry", {
                "p2p_failing": [c.test_id for c in state.todo_list.p2p_failing],
            })
            cb = rebuild_context_from_regressions(ws, state.todo_list)

            # Merge: preserve prior context files so the agent keeps visibility into
            # what it was already working on. Re-read from disk to get current state
            # (agent may have modified these files during the prior IMPLEMENT pass).
            new_paths = {f["path"] for f in cb.task_adjacent_files}
            prior_files_refreshed = []
            for pf in state.context_bundle.task_adjacent_files:
                if pf["path"] in new_paths:
                    continue
                abs_path = os.path.join(ws, pf["path"])
                if not os.path.isfile(abs_path):
                    continue
                content = _read_file_smart(abs_path, anchor=0, head_lines=120)
                if content:
                    prior_files_refreshed.append({
                        "path": pf["path"],
                        "content": content,
                        "why": "from prior attempt (current on-disk state — do not revert)",
                    })
                if len(prior_files_refreshed) >= 5:
                    break

            from cloud_agent.config import settings
            ranked = _rank_sections(
                cb, state.task_text, state.todo_list, ws, settings.discovery_model
            )

            cb = ContextBundle(
                repo_rules=cb.repo_rules or state.context_bundle.repo_rules,
                build_and_test_commands=cb.build_and_test_commands,
                task_adjacent_files=cb.task_adjacent_files + prior_files_refreshed,
                ranked_sections=ranked,
            )

            self.tracer.emit("gather_context.done", {
                "context_files": [f["path"] for f in cb.task_adjacent_files],
                "prior_files_preserved": [f["path"] for f in prior_files_refreshed],
                "ranked_section_files": [r["path"] for r in ranked],
                "mode": "regression",
            })
            return NodeResult(
                next_node="IMPLEMENT",
                state_update={"context_bundle": cb},
                status="ok",
            )

        # First entry (or f2p retry re-seeding context):
        # Run all tests, collect tracebacks, build context bundle
        fail_to_pass = state.fail_to_pass
        pass_to_pass = state.pass_to_pass

        self.tracer.emit("gather_context.start", {
            "fail_to_pass": fail_to_pass,
            "pass_to_pass_count": len(pass_to_pass),
        })

        todo_list, cb = build_bugfix_context(ws, fail_to_pass, pass_to_pass)

        from cloud_agent.config import settings
        ranked = _rank_sections(
            cb, state.task_text, todo_list, ws, settings.discovery_model
        )
        cb = ContextBundle(
            repo_rules=cb.repo_rules,
            build_and_test_commands=cb.build_and_test_commands,
            task_adjacent_files=cb.task_adjacent_files,
            ranked_sections=ranked,
        )

        self.tracer.emit("gather_context.done", {
            "f2p_failing": [c.test_id for c in todo_list.f2p_failing],
            "p2p_failing": [c.test_id for c in todo_list.p2p_failing],
            "context_files": [f["path"] for f in cb.task_adjacent_files],
            "ranked_section_files": [r["path"] for r in ranked],
            "mode": "initial",
        })

        # Capture which p2p tests were already failing at baseline (before agent makes changes).
        # VERIFY uses this to avoid treating pre-existing failures as regressions.
        baseline_failing = [
            c.test_id for c in todo_list.cases
            if c.category == "pass_to_pass" and c.status == "failing"
        ]
        if baseline_failing:
            self.tracer.emit("gather_context.baseline_p2p_failing", {
                "count": len(baseline_failing),
                "test_ids": baseline_failing[:10],  # log first 10
            })

        return NodeResult(
            next_node="IMPLEMENT",
            state_update={
                "todo_list": todo_list,
                "context_bundle": cb,
                "baseline_p2p_failing": baseline_failing,
            },
            status="ok",
        )

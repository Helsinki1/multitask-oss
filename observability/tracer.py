"""JSONL trace event emitter. One file per session under traces/."""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Tracer:
    def __init__(self, session_id: str, traces_dir: str = "traces") -> None:
        self.session_id = session_id
        self.traces_dir = traces_dir
        os.makedirs(traces_dir, exist_ok=True)
        self._path = os.path.join(traces_dir, f"{session_id}.jsonl")

    def emit(self, event_type: str, data: dict[str, Any] = {}) -> None:
        record = {
            "ts": _now_iso(),
            "session_id": self.session_id,
            "type": event_type,
            **data,
        }
        line = json.dumps(record, default=str)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # Also print to stderr so the user can see progress
        self._print(event_type, data)

    def _print(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "node.start":
            print(f"\n[{data.get('node', '')}] starting...", file=sys.stderr)
        elif event_type == "node.complete":
            status = data.get("status", "ok")
            next_node = data.get("next", "")
            print(f"[{data.get('node', '')}] {status} → {next_node}", file=sys.stderr)
        elif event_type == "node.error":
            print(f"[ERROR] {data.get('node', '')}: {data.get('error', '')}", file=sys.stderr)
        elif event_type == "tool_call":
            name = data.get("name", "")
            preview = str(data.get("result_preview", ""))[:120]
            print(f"  tool: {name} → {preview}", file=sys.stderr)
        elif event_type == "tool_error":
            print(f"  tool ERROR: {data.get('name', '')}: {data.get('error', '')}", file=sys.stderr)
        elif event_type == "model_response":
            turns = data.get("turn", "?")
            cost = data.get("cost_usd", 0)
            tokens_in = data.get("input_tokens", 0)
            tokens_out = data.get("output_tokens", 0)
            print(
                f"  llm turn {turns}: {tokens_in}→{tokens_out} tokens  ${cost:.4f}",
                file=sys.stderr,
            )
        elif event_type == "is_done_check":
            done = data.get("is_done", False)
            reason = data.get("reason", "")
            print(f"  is_done={done} ({reason})", file=sys.stderr)
        elif event_type == "subsession.done":
            print(f"  subsession complete: {data.get('status', '')}", file=sys.stderr)
        elif event_type == "verify.result":
            f2p_fail = data.get("f2p_failing", [])
            p2p_new = data.get("p2p_newly_failing", [])
            p2p_base = data.get("p2p_baseline_still_failing", 0)
            base_suffix = f"  p2p_baseline_still_failing={p2p_base}" if p2p_base else ""
            print(f"  verify: f2p_failing={f2p_fail}  p2p_newly_failing={p2p_new}{base_suffix}", file=sys.stderr)
        elif event_type == "history.compressed":
            print(
                f"  [compression] turn {data.get('turn')}: stale tool results truncated"
                f" (keeping {data.get('keep_turns')} recent turns)",
                file=sys.stderr,
            )
        elif event_type in ("task.cancelled", "task.budget_exhausted"):
            print(f"[TASK] {event_type}", file=sys.stderr)

    @property
    def path(self) -> str:
        return self._path

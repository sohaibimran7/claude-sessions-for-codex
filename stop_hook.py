#!/usr/bin/env python3
"""Claude Code Stop hook: when a watched chat finishes a turn, append its final message to
~/.claude-bridge/events.jsonl so a Codex orchestrator can pick it up with claude_wait.

Chats are watched when the orchestrator messages or starts them (or via claude_watch).
Unwatched chats are ignored, so nothing is logged for everyday use."""
import json
import os
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("CLAUDE_BRIDGE_HOME", Path.home() / ".claude-bridge"))
MAX_LOG_BYTES = 20_000_000


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    sid = data.get("session_id")
    if not sid or data.get("agent_id"):  # skip subagents; report the main conversation only
        return
    try:
        watched = set(json.loads((STATE / "watched.json").read_text()))
    except (OSError, ValueError):
        return
    if sid not in watched:
        return
    events = STATE / "events.jsonl"
    try:
        if events.stat().st_size > MAX_LOG_BYTES:
            events.rename(events.with_suffix(f".{int(time.time())}.jsonl"))
    except FileNotFoundError:
        pass
    ev = {"kind": "turn_end", "session_id": sid, "cwd": data.get("cwd", ""),
          "text": (data.get("last_assistant_message") or "")[:8000], "ts": int(time.time() * 1000)}
    with open(events, "a") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

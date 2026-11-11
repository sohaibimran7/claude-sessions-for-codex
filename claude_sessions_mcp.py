#!/usr/bin/env python3
"""MCP server that lets an OpenAI Codex thread (including Codex live/voice mode) orchestrate
Claude Code chats on this machine: list, read, message, start, and wait for their reports.

Transport: MCP over stdio (newline-delimited JSON-RPC 2.0). Standard library only.

Routing for claude_send:
  * open chat (a live Claude Code process with an inbox socket) -> cross-session message
    through that socket; it arrives between tool calls, or starts a turn if the chat is idle.
  * closed chat -> a background `claude -p --resume <id>` run. Never both: resuming a chat
    that is running elsewhere would interleave two writers in one transcript.
  Optional `model` / `effort` become `--model` / `--effort` on background runs (closed chats and
  claude_start_session). For an open chat they are an error before anything is sent: the inbox
  only carries messages and cannot change the running process's settings.

Reports come from stop_hook.py, a Claude Code Stop hook that appends the final message of
every watched chat to events.jsonl; claude_wait returns new events with short timeouts so a
voice session stays responsive.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import sys
import time
import uuid
from pathlib import Path

HOME = Path.home()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
STATE = Path(os.environ.get("CLAUDE_BRIDGE_HOME", HOME / ".claude-bridge"))
EVENTS = STATE / "events.jsonl"
WATCHED = STATE / "watched.json"
JOBS = STATE / "jobs"
DESKTOP_INDEX = HOME / "Library/Application Support/Claude/claude-code-sessions"
SENDER_NAME = os.environ.get("CLAUDE_BRIDGE_NAME", "Codex orchestrator")
PROTOCOL_VERSION = "2025-06-18"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")  # `claude --effort` values (Claude Code 2.1.294)
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,199}$")  # aliases and full ids, no flags/whitespace


# --------------------------------------------------------------------------- helpers

def now_ms() -> int:
    return int(time.time() * 1000)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claude_bin() -> str:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(HOME / ".local/bin/claude")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def watched() -> set[str]:
    return set(read_json(WATCHED, []))


def set_watched(ids: set[str]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = WATCHED.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    os.replace(tmp, WATCHED)


def watch(session_id: str, on: bool = True) -> None:
    ids = watched()
    (ids.add if on else ids.discard)(session_id)
    set_watched(ids)


def append_event(ev: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    ev.setdefault("ts", now_ms())
    with open(EVENTS, "a") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- session index

def live_sessions() -> dict[str, dict]:
    """sessionId -> registry record, for Claude Code processes that are running now."""
    out = {}
    for f in (CLAUDE_HOME / "sessions").glob("*.json"):
        rec = read_json(f, None)
        if not isinstance(rec, dict) or not rec.get("sessionId"):
            continue
        if isinstance(rec.get("pid"), int) and pid_alive(rec["pid"]):
            out[rec["sessionId"]] = rec
    return out


def desktop_records() -> dict[str, dict]:
    """cliSessionId -> Claude desktop app record (title, archived, local id)."""
    out = {}
    for f in DESKTOP_INDEX.glob("*/*/local_*.json"):
        rec = read_json(f, None)
        if isinstance(rec, dict) and rec.get("cliSessionId"):
            out[rec["cliSessionId"]] = rec
    return out


_TITLES: dict[tuple[str, float], tuple[str | None, str]] = {}


def transcript_title(path: Path) -> tuple[str | None, str]:
    """(customTitle record or first human prompt, working folder), cached per file version."""
    key = (str(path), path.stat().st_mtime)
    if key not in _TITLES:
        _TITLES[key] = _scan_title(path)
    return _TITLES[key]


def _scan_title(path: Path) -> tuple[str | None, str]:
    title, cwd = None, ""
    try:
        with open(path) as fh:
            for i, line in enumerate(fh):
                if not cwd and '"cwd"' in line:
                    m = re.search(r'"cwd":\s*"((?:[^"\\]|\\.)*)"', line)
                    cwd = json.loads('"' + m.group(1) + '"') if m else ""
                if '"custom-title"' in line:
                    try:
                        title = json.loads(line).get("customTitle") or title
                    except ValueError:
                        pass
                elif title is None and '"type":"user"' in line.replace(" ", "") and i < 400:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    c = (r.get("message") or {}).get("content")
                    if isinstance(c, str) and not r.get("isMeta") and not r.get("isCompactSummary"):
                        c = re.sub(r"<[^>]+>", " ", c)
                        title = " ".join(c.split())[:80] or None
                if i > 4000 and title and cwd:
                    break
    except OSError:
        pass
    return title, cwd


def all_sessions() -> list[dict]:
    live = live_sessions()
    desk = desktop_records()
    superseded = {p for d in desk.values() for p in d.get("priorCliSessionIds") or []}
    rows = []
    for f in (CLAUDE_HOME / "projects").glob("*/*.jsonl"):
        sid = f.stem
        if not UUID_RE.match(sid) or sid in superseded:
            continue
        d = desk.get(sid, {})
        lv = live.get(sid)
        t_title, t_cwd = transcript_title(f)
        rows.append({
            "session_id": sid,
            "desktop_id": d.get("sessionId"),
            "title": (lv or {}).get("name") or d.get("title") or t_title or sid,
            "cwd": d.get("cwd") or (lv or {}).get("cwd") or t_cwd,
            "state": ("open, " + lv.get("status", "?")) if lv else "closed",
            "archived": bool(d.get("isArchived")),
            "updated": int(f.stat().st_mtime * 1000),
            "watched": sid in watched(),
            "_path": str(f),
            "_live": lv,
        })
    # live sessions with no transcript yet
    seen = {r["session_id"] for r in rows}
    for sid, lv in live.items():
        if sid not in seen:
            rows.append({"session_id": sid, "desktop_id": lv.get("hostSessionId"), "title": lv.get("name") or sid,
                         "cwd": lv.get("cwd", ""), "state": "open, " + lv.get("status", "?"), "archived": False,
                         "updated": lv.get("updatedAt", 0), "watched": sid in watched(), "_path": None, "_live": lv})
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows


def resolve(ref: str) -> dict:
    """Accept a session id, a desktop id (local_...), or a unique title fragment."""
    ref = (ref or "").strip()
    rows = all_sessions()
    for r in rows:
        if ref in (r["session_id"], r["desktop_id"]):
            return r
    hits = [r for r in rows if ref.lower() in r["title"].lower()] if ref else []
    exact = [r for r in hits if r["title"].lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no Claude chat matches {ref!r}; call claude_list_sessions")
    listing = "; ".join(f"{h['title']} ({h['session_id'][:8]}, {h['state']})" for h in hits[:8])
    raise ValueError(f"{len(hits)} chats match {ref!r}: {listing}. Use a session id.")


def public(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- transcripts

def render_turns(path: str, turns: int, max_chars: int) -> str:
    """The last `turns` human prompts with everything after them, tool activity condensed."""
    recs = []
    with open(path) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") in ("user", "assistant") and not r.get("isVisibleInTranscriptOnly"):
                recs.append(r)
    starts = [i for i, r in enumerate(recs) if r["type"] == "user" and isinstance(r["message"].get("content"), str)
              and not r.get("isMeta") and not r.get("isCompactSummary")]
    first = starts[-turns] if len(starts) >= turns else 0
    out = []
    for r in recs[first:]:
        c = r["message"].get("content")
        ts = (r.get("timestamp") or "")[:16].replace("T", " ")
        if isinstance(c, str):
            out.append(f"\n[{ts}] {'USER' if r['type'] == 'user' else 'CLAUDE'}: {c.strip()}")
            continue
        for b in c or []:
            t = b.get("type")
            if t == "text" and b.get("text", "").strip():
                out.append(f"\n[{ts}] {'USER' if r['type'] == 'user' else 'CLAUDE'}: {b['text'].strip()}")
            elif t == "tool_use":
                arg = json.dumps(b.get("input"), ensure_ascii=False)
                out.append(f"  - tool {b.get('name')}: {' '.join(arg.split())[:160]}")
            elif t == "tool_result":
                cc = b.get("content")
                txt = cc if isinstance(cc, str) else " ".join(x.get("text", "") for x in cc or [] if isinstance(x, dict))
                out.append(f"    -> {'ERROR ' if b.get('is_error') else ''}{' '.join(txt.split())[:160]}")
    text = "\n".join(out).strip()
    if len(text) > max_chars:
        text = "[... earlier part of these turns omitted ...]\n" + text[-max_chars:]
    return text or "(no messages yet)"


# --------------------------------------------------------------------------- sending

class Inbox:
    """This server's own socket. A background thread records delivery statuses and messages
    that Claude chats send back to the orchestrator (via their SendMessage tool)."""

    def __init__(self):
        self.path = None
        self.sock = None
        self.status: dict[str, str] = {}
        self.lock = threading.Lock()

    def ensure(self) -> str:
        if self.path:
            return self.path
        d = STATE / "socks"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        self.path = str(d / f"{os.getpid()}.sock")
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(64)
        threading.Thread(target=self._serve, daemon=True).start()
        return self.path

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn) -> None:
        buf = b""
        with conn:
            conn.settimeout(30)
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
        for line in buf.decode(errors="replace").splitlines():
            try:
                f = json.loads(line)
            except ValueError:
                continue
            if f.get("orig_msg_id"):
                st = f.get("state") or f.get("status") or "?"
                with self.lock:
                    self.status[f["orig_msg_id"]] = st
                if st not in ("delivered", "?"):  # held for approval, refused, dropped, expired
                    append_event({"kind": "delivery", "session_id": sender_session(f.get("from", "")),
                                  "text": f"message {st}" + (f" ({f['drop_reason']})" if f.get("drop_reason") else "")})
            elif f.get("type") == "user":
                raw = (f.get("message") or {}).get("content", "")
                raw = raw if isinstance(raw, str) else json.dumps(raw)
                m = re.search(r"<cross-session-message[^>]*>\n?(.*?)\n?</cross-session-message>", raw, re.S)
                sender = sender_session(f.get("from", ""))
                append_event({"kind": "message", "session_id": sender, "from": f.get("from"),
                              "text": (m.group(1) if m else raw)[:8000]})

    def status_for(self, msg_id: str, timeout: float) -> str | None:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if msg_id in self.status:
                    return self.status[msg_id]
            time.sleep(0.1)
        return None

    def close(self):
        if self.path:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def sender_session(addr: str) -> str | None:
    path = addr[4:] if addr.startswith("uds:") else addr
    for f in (CLAUDE_HOME / "sessions").glob("*.json"):
        rec = read_json(f, {})
        if rec.get("messagingSocketPath") == path:
            return rec.get("sessionId")
    return None


INBOX = Inbox()


def send_live(live: dict, text: str) -> dict:
    sock_path = live.get("messagingSocketPath")
    if not sock_path or not os.path.exists(sock_path):
        raise RuntimeError("the chat is running but has no inbox socket (cross-session messaging unavailable)")
    me = "uds:" + INBOX.ensure()
    msg_id = str(uuid.uuid4())
    body = text.replace("</cross-session-message>", "</cross-session-message >") + (
        "\n\n(From the Codex orchestrator. Your normal final reply is delivered to it automatically, "
        "so just answer here; no need to use SendMessage.)")
    frame = {"msgV": 1, "msg_id": msg_id, "type": "user",
             "message": {"role": "user", "content": f'<cross-session-message from="{me}" from-name="{SENDER_NAME}" '
                                                     f'from-mode="prompting">\n{body}\n</cross-session-message>'},
             "priority": "next", "from": me}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(sock_path)
        s.sendall(json.dumps(frame).encode() + b"\n")
    state = INBOX.status_for(msg_id, 3.0)
    return {"route": "inbox", "msg_id": msg_id, "delivery": state or "sent",
            "note": "if the chat holds or refuses it, claude_wait reports a 'delivery' event"}


def spawn_headless(args: list[str], cwd: str, session_id: str, label: str) -> dict:
    """Run `claude -p ...` detached; a small runner records the outcome as an event."""
    JOBS.mkdir(parents=True, exist_ok=True)
    job = uuid.uuid4().hex[:12]
    out = JOBS / f"{job}.json"
    runner = (
        "import json,subprocess,sys,time\n"
        "args,out,sid,label=json.loads(sys.argv[1]),sys.argv[2],sys.argv[3],sys.argv[4]\n"
        "p=subprocess.run(args,capture_output=True,text=True)\n"
        "try:\n d=json.loads(p.stdout)\n"
        "except Exception:\n d={'is_error':True,'result':(p.stdout+p.stderr)[-4000:]}\n"
        "open(out,'w').write(json.dumps({'exit':p.returncode,**d}))\n"
        f"ev={{'kind':'job_done' if p.returncode==0 and not d.get('is_error') else 'job_failed','session_id':sid,"
        "'title':label,'text':str(d.get('result',''))[:8000],'ts':int(time.time()*1000)}\n"
        f"open({str(EVENTS)!r},'a').write(json.dumps(ev,ensure_ascii=False)+'\\n')\n"
    )
    subprocess.Popen([sys.executable, "-c", runner, json.dumps(args), str(out), session_id, label],
                     cwd=cwd if os.path.isdir(cwd) else str(HOME), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"route": "headless", "job": job, "note": "running in the background; claude_wait reports when it finishes"}


# --------------------------------------------------------------------------- model / effort

def selection_args(a: dict) -> tuple[list[str], dict]:
    """Validate optional `model` and `effort` inputs.

    Returns (extra CLI flags, requested-settings metadata). Both are empty when neither key is
    given, so the CLI keeps its own defaults. Anything else that is not a usable value (null,
    empty, non-string, unknown effort level, flag-like model) is an error: the bridge never
    substitutes a default for a value the caller tried to set."""
    flags: list[str] = []
    requested: dict = {}
    if "model" in a:
        model = a["model"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string such as 'fable', 'opus', 'sonnet' or a full model "
                             "name; omit it to use the CLI's default")
        model = model.strip()
        if not MODEL_RE.match(model):
            raise ValueError(f"model {model!r} is not a valid model alias or name (letters, digits, . _ : / @ [ ] -; "
                             "no spaces or leading '-')")
        flags += ["--model", model]
        requested["model"] = model
    if "effort" in a:
        effort = a["effort"]
        if not isinstance(effort, str) or not effort.strip():
            raise ValueError(f"effort must be one of {', '.join(EFFORT_LEVELS)}; omit it to use the CLI's default")
        effort = effort.strip().lower()
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"effort {a['effort']!r} is not one of {', '.join(EFFORT_LEVELS)}")
        flags += ["--effort", effort]
        requested["effort"] = effort
    return flags, requested


REQUESTED_STATUS = "passed to claude as --model/--effort flags; the CLI validates them, so this is the request, not " \
                   "confirmation of what ran (a rejected value shows up as a job_failed report in claude_wait)"


def reject_live_override(r: dict, requested: dict) -> None:
    """An open chat's inbox only carries messages; it cannot change that process's settings."""
    if r["_live"] and requested:
        what = " and ".join(f"{k} {v!r}" for k, v in requested.items())
        raise ValueError(
            f"{r['title']} ({r['session_id']}) is {r['state']}: {what} cannot be applied to a chat that is already "
            "running, and nothing was sent. Its inbox only delivers messages; it cannot change the model or effort "
            "of the running process, and the bridge will not restart it or resume it in parallel. Either send the "
            "message without model/effort to use the chat's current settings, start a new chat with "
            "claude_start_session, or wait until the chat is closed and resume it with model/effort.")


# --------------------------------------------------------------------------- tools

def t_list(a: dict) -> str:
    rows = all_sessions()
    q = (a.get("query") or "").lower()
    proj = a.get("project")
    if q:
        rows = [r for r in rows if q in r["title"].lower() or q in r["cwd"].lower()]
    if proj:
        rp = os.path.realpath(os.path.expanduser(proj))
        rows = [r for r in rows if r["cwd"] and os.path.realpath(r["cwd"]) == rp]
    if a.get("only_open"):
        rows = [r for r in rows if r["state"].startswith("open")]
    if not a.get("include_archived"):
        rows = [r for r in rows if not r["archived"]]
    rows = rows[: int(a.get("limit") or 30)]
    lines = [f"{len(rows)} Claude chats (most recent first):"]
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"] / 1000))
        lines.append(f"- {r['title']} | id {r['session_id']} | {r['state']}{' | watched' if r['watched'] else ''} "
                     f"| {r['cwd'].replace(str(HOME), '~')} | last activity {when}")
    return "\n".join(lines)


def t_read(a: dict) -> str:
    r = resolve(a["session"])
    if not r["_path"]:
        return f"{r['title']} is open but has no saved messages yet."
    head = f"{r['title']} ({r['state']}, {r['cwd'].replace(str(HOME), '~')})"
    return head + "\n" + render_turns(r["_path"], int(a.get("turns") or 3), int(a.get("max_chars") or 12000))


def t_send(a: dict) -> str:
    r = resolve(a["session"])
    text = a["message"]
    flags, requested = selection_args(a)  # validated before anything is watched, sent or started
    reject_live_override(r, requested)
    watch(r["session_id"])
    if r["_live"]:
        res = send_live(r["_live"], text)
    else:
        if not r["_path"]:
            raise ValueError("that chat has no transcript to resume")
        mode = a.get("permission_mode") or "auto"
        args = [claude_bin(), "-p", "--resume", r["session_id"], "--output-format", "json",
                "--permission-mode", mode, *flags, text]
        res = spawn_headless(args, r["cwd"] or str(HOME), r["session_id"], r["title"])
        if requested:
            res["requested"] = dict(requested, status=REQUESTED_STATUS)
    res.update(chat=r["title"], session_id=r["session_id"])
    return json.dumps(res)


def t_start(a: dict) -> str:
    cwd = os.path.expanduser(a.get("cwd") or str(HOME))
    if not os.path.isdir(cwd):
        raise ValueError(f"folder does not exist: {cwd}")
    flags, requested = selection_args(a)
    sid = str(uuid.uuid4())
    title = a.get("title") or " ".join(a["prompt"].split())[:60]
    watch(sid)
    args = [claude_bin(), "-p", "--session-id", sid, "--name", title, "--output-format", "json",
            "--permission-mode", a.get("permission_mode") or "auto", *flags, a["prompt"]]
    res = spawn_headless(args, cwd, sid, title)
    if requested:
        res["requested"] = dict(requested, status=REQUESTED_STATUS)
    res.update(session_id=sid, title=title, cwd=cwd)
    return json.dumps(res)


def t_watch(a: dict) -> str:
    r = resolve(a["session"])
    on = a.get("on", True)
    watch(r["session_id"], bool(on))
    return f"{'Watching' if on else 'Stopped watching'} {r['title']} ({r['session_id']})."


def t_wait(a: dict) -> str:
    """Return report events after `cursor`, waiting up to timeout_s for the first one."""
    timeout = min(float(a.get("timeout_s", 20)), 120.0)
    cursor = int(a.get("cursor") or 0)
    want = None
    if a.get("sessions"):
        want = {s if UUID_RE.match(s) else resolve(s)["session_id"] for s in a["sessions"]}
    end = time.time() + timeout
    while True:
        evs, size = [], 0
        try:
            size = EVENTS.stat().st_size
            with open(EVENTS) as fh:
                fh.seek(min(cursor, size))
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if want and ev.get("session_id") not in want:
                        continue
                    evs.append(ev)
        except FileNotFoundError:
            pass
        if evs or time.time() >= end:
            break
        time.sleep(0.5)
    if not evs:
        return json.dumps({"events": [], "cursor": size, "note": f"no new reports in {timeout:.0f}s"})
    # a background run reports twice (Stop hook + runner); keep one unless the run failed
    ended = {(e.get("session_id"), e.get("text")) for e in evs if e.get("kind") == "turn_end"}
    evs = [e for e in evs if not (e.get("kind") == "job_done" and (e.get("session_id"), e.get("text")) in ended)]
    names = {r["session_id"]: r["title"] for r in all_sessions()}
    for ev in evs:
        ev["time"] = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0) / 1000))
        ev.setdefault("title", names.get(ev.get("session_id"), ""))
    return json.dumps({"events": evs, "cursor": size}, ensure_ascii=False)


TOOLS = {
    "claude_list_sessions": (t_list, "List Claude Code chats on this machine (open and closed), most recent first.", {
        "query": {"type": "string", "description": "filter by title or folder text"},
        "project": {"type": "string", "description": "only chats whose folder is this path"},
        "only_open": {"type": "boolean", "description": "only chats that are running right now"},
        "include_archived": {"type": "boolean"},
        "limit": {"type": "integer", "default": 30}}, []),
    "claude_read_session": (t_read, "Read the recent turns of a Claude chat (tool activity condensed).", {
        "session": {"type": "string", "description": "session id, desktop id, or unique title fragment"},
        "turns": {"type": "integer", "default": 3, "description": "how many recent user turns"},
        "max_chars": {"type": "integer", "default": 12000}}, ["session"]),
    "claude_send": (t_send, "Send a message to a Claude chat. Open chats get it through their inbox (it starts a turn if "
                            "idle, or arrives between tool calls if busy); closed chats are resumed in the background. "
                            "The chat is watched afterwards, so claude_wait returns its reply.", {
        "session": {"type": "string", "description": "session id, desktop id, or unique title fragment"},
        "message": {"type": "string"},
        "permission_mode": {"type": "string", "enum": ["auto", "acceptEdits", "manual", "dontAsk", "plan"],
                            "description": "closed chats only: mode for the background run (default auto)"},
        "model": {"type": "string", "description": "closed chats only: model for the background resume, an alias "
                                                   "('fable', 'opus', 'sonnet') or full model name; omit to keep "
                                                   "the CLI default. Rejected (nothing sent) if the chat is open."},
        "effort": {"type": "string", "enum": list(EFFORT_LEVELS),
                   "description": "closed chats only: effort level for the background resume; omit to keep the CLI "
                                  "default. Rejected (nothing sent) if the chat is open."}},
                   ["session", "message"]),
    "claude_start_session": (t_start, "Start a new Claude chat in a folder with a first prompt (runs in the background; "
                                      "watched; claude_wait returns its reply).", {
        "cwd": {"type": "string", "description": "project folder"},
        "prompt": {"type": "string"},
        "title": {"type": "string"},
        "permission_mode": {"type": "string", "enum": ["auto", "acceptEdits", "manual", "dontAsk", "plan"]},
        "model": {"type": "string", "description": "model for the new chat, an alias ('fable', 'opus', 'sonnet') or "
                                                   "full model name; omit to use the CLI default"},
        "effort": {"type": "string", "enum": list(EFFORT_LEVELS),
                   "description": "effort level for the new chat; omit to use the CLI default"}},
                             ["prompt"]),
    "claude_watch": (t_watch, "Start or stop receiving end-of-turn reports from a Claude chat.", {
        "session": {"type": "string"}, "on": {"type": "boolean", "default": True}}, ["session"]),
    "claude_wait": (t_wait, "Wait for reports from watched Claude chats: each finished turn's final message, finished or "
                            "failed background runs, and messages chats sent to you. Keep timeout_s short (<=30) during "
                            "voice; pass back the returned cursor next time to get only newer reports.", {
        "sessions": {"type": "array", "items": {"type": "string"}, "description": "limit to these chats"},
        "timeout_s": {"type": "number", "default": 20},
        "cursor": {"type": "integer", "description": "cursor from the previous claude_wait (0 = from the start)"}}, []),
}


# --------------------------------------------------------------------------- MCP stdio loop

def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req: dict) -> None:
    method, mid = req.get("method"), req.get("id")
    if mid is None:
        return  # notification
    if method == "initialize":
        reply(mid, {"protocolVersion": (req.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "claude-sessions", "version": "0.1.0"},
                    "instructions": "Tools to orchestrate Claude Code chats. Reports from chats are pulled with "
                                    "claude_wait (short timeouts), never pushed."})
    elif method == "ping":
        reply(mid, {})
    elif method == "tools/list":
        reply(mid, {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p,
                                                                             "required": req_}}
                              for n, (_, d, p, req_) in TOOLS.items()]})
    elif method == "tools/call":
        params = req.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            reply(mid, error={"code": -32602, "message": f"unknown tool {name}"})
            return
        try:
            text, is_err = TOOLS[name][0](args), False
        except Exception as e:  # report tool errors to the model rather than failing the call
            text, is_err = f"{type(e).__name__}: {e}", True
        reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
    else:
        reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    INBOX.ensure()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            handle(req)
    finally:
        INBOX.close()


if __name__ == "__main__":
    main()

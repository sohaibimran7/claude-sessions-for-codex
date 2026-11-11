#!/usr/bin/env python3
"""Unit tests for model / effort selection in claude_sessions_mcp.py. Standard library only.

Run:  python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Point the bridge's state at a scratch folder before importing, so tests never touch ~/.claude-bridge.
_TMP = tempfile.TemporaryDirectory()
os.environ["CLAUDE_BRIDGE_HOME"] = _TMP.name
os.environ["CLAUDE_BIN"] = "/fake/bin/claude"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_sessions_mcp as mcp  # noqa: E402

SID_CLOSED = "11111111-2222-3333-4444-555555555555"
SID_LIVE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def closed_row():
    return {"session_id": SID_CLOSED, "desktop_id": None, "title": "closed chat", "cwd": _TMP.name,
            "state": "closed", "archived": False, "updated": 0, "watched": False,
            "_path": "/fake/transcript.jsonl", "_live": None}


def live_row():
    return {"session_id": SID_LIVE, "desktop_id": None, "title": "live chat", "cwd": _TMP.name,
            "state": "open, idle", "archived": False, "updated": 0, "watched": False,
            "_path": "/fake/live.jsonl", "_live": {"sessionId": SID_LIVE, "messagingSocketPath": "/fake.sock"}}


class Base(unittest.TestCase):
    """Patch every side effect: no process is spawned, no socket is written, no state file changes."""

    def setUp(self):
        self.spawned = []
        self.sent = []
        self.watched = []
        patches = [
            mock.patch.object(mcp, "spawn_headless", side_effect=self._spawn),
            mock.patch.object(mcp, "send_live", side_effect=self._send_live),
            mock.patch.object(mcp, "watch", side_effect=lambda sid, on=True: self.watched.append(sid)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _spawn(self, args, cwd, session_id, label):
        self.spawned.append({"args": list(args), "cwd": cwd, "session_id": session_id, "label": label})
        return {"route": "headless", "job": "job123", "note": "running"}

    def _send_live(self, live, text):
        self.sent.append({"live": live, "text": text})
        return {"route": "inbox", "msg_id": "m1", "delivery": "delivered"}

    def assert_nothing_happened(self):
        self.assertEqual(self.spawned, [], "a background run was started")
        self.assertEqual(self.sent, [], "a message was delivered")
        self.assertEqual(self.watched, [], "the chat was watched")


# --------------------------------------------------------------------------- claude_start_session

class StartSession(Base):
    def start(self, **extra):
        out = json.loads(mcp.t_start({"prompt": "do the thing", "cwd": _TMP.name, **extra}))
        self.assertEqual(len(self.spawned), 1)
        return self.spawned[0]["args"], out

    def test_no_override_keeps_prior_argv(self):
        args, out = self.start()
        sid = self.spawned[0]["session_id"]
        self.assertEqual(args, ["/fake/bin/claude", "-p", "--session-id", sid, "--name", "do the thing",
                                "--output-format", "json", "--permission-mode", "auto", "do the thing"])
        self.assertNotIn("--model", args)
        self.assertNotIn("--effort", args)
        self.assertNotIn("requested", out)

    def test_model_only(self):
        args, out = self.start(model="opus")
        self.assertEqual(args[-3:], ["--model", "opus", "do the thing"])
        self.assertNotIn("--effort", args)
        self.assertEqual(out["requested"]["model"], "opus")
        self.assertNotIn("effort", out["requested"])
        self.assertIn("status", out["requested"])

    def test_effort_only(self):
        args, out = self.start(effort="xhigh")
        self.assertEqual(args[-3:], ["--effort", "xhigh", "do the thing"])
        self.assertNotIn("--model", args)
        self.assertEqual(out["requested"]["effort"], "xhigh")
        self.assertNotIn("model", out["requested"])

    def test_model_and_effort(self):
        args, out = self.start(model="claude-fable-5-1", effort="max", permission_mode="plan")
        self.assertEqual(args[-5:], ["--model", "claude-fable-5-1", "--effort", "max", "do the thing"])
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        self.assertEqual(out["requested"]["model"], "claude-fable-5-1")
        self.assertEqual(out["requested"]["effort"], "max")

    def test_flags_are_separate_argv_items_not_shell(self):
        args, _ = self.start(model="opus[1m]", effort="low")
        self.assertIsInstance(args, list)
        self.assertIn("opus[1m]", args)  # passed verbatim as one argv element
        self.assertTrue(all(isinstance(x, str) for x in args))

    def test_effort_case_is_normalised(self):
        args, out = self.start(effort=" High ")
        self.assertEqual(args[-3:], ["--effort", "high", "do the thing"])
        self.assertEqual(out["requested"]["effort"], "high")

    def test_every_effort_level_accepted(self):
        for level in mcp.EFFORT_LEVELS:
            self.spawned.clear()
            args, _ = self.start(effort=level)
            self.assertEqual(args[args.index("--effort") + 1], level)

    def test_invalid_effort_rejected_without_side_effects(self):
        for bad in ("ultra", "", "   ", None, 3, True, ["high"], {"level": "high"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    mcp.t_start({"prompt": "x", "cwd": _TMP.name, "effort": bad})
                self.assertIn("effort", str(cm.exception))
                self.assertIn("low, medium, high, xhigh, max", str(cm.exception))
                self.assert_nothing_happened()

    def test_invalid_model_rejected_without_side_effects(self):
        for bad in ("", "   ", None, 7, False, ["opus"], {"name": "opus"}, "--effort", "-x", "op us", "a\nb", "x" * 201):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    mcp.t_start({"prompt": "x", "cwd": _TMP.name, "model": bad})
                self.assertIn("model", str(cm.exception))
                self.assert_nothing_happened()

    def test_valid_model_with_bad_effort_starts_nothing(self):
        with self.assertRaises(ValueError):
            mcp.t_start({"prompt": "x", "cwd": _TMP.name, "model": "opus", "effort": "turbo"})
        self.assert_nothing_happened()

    def test_full_model_ids_accepted(self):
        for name in ("sonnet", "claude-opus-5-5", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "opus[1m]",
                     "my/vertex@model"):
            self.spawned.clear()
            args, _ = self.start(model=name)
            self.assertEqual(args[args.index("--model") + 1], name)


# --------------------------------------------------------------------------- claude_send, closed chat

class SendClosed(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(mcp, "resolve", return_value=closed_row())
        p.start()
        self.addCleanup(p.stop)

    def send(self, **extra):
        out = json.loads(mcp.t_send({"session": "closed chat", "message": "hello", **extra}))
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.sent, [])
        return self.spawned[0]["args"], out

    def test_no_override_keeps_prior_argv(self):
        args, out = self.send()
        self.assertEqual(args, ["/fake/bin/claude", "-p", "--resume", SID_CLOSED, "--output-format", "json",
                                "--permission-mode", "auto", "hello"])
        self.assertNotIn("requested", out)
        self.assertEqual(self.watched, [SID_CLOSED])

    def test_model_only(self):
        args, out = self.send(model="sonnet")
        self.assertEqual(args[-3:], ["--model", "sonnet", "hello"])
        self.assertNotIn("--effort", args)
        self.assertEqual(out["requested"], {"model": "sonnet", "status": mcp.REQUESTED_STATUS})

    def test_effort_only(self):
        args, out = self.send(effort="medium")
        self.assertEqual(args[-3:], ["--effort", "medium", "hello"])
        self.assertNotIn("--model", args)
        self.assertEqual(out["requested"]["effort"], "medium")

    def test_model_and_effort(self):
        args, out = self.send(model="fable", effort="high", permission_mode="acceptEdits")
        self.assertEqual(args, ["/fake/bin/claude", "-p", "--resume", SID_CLOSED, "--output-format", "json",
                                "--permission-mode", "acceptEdits", "--model", "fable", "--effort", "high", "hello"])
        self.assertEqual(out["requested"]["model"], "fable")
        self.assertEqual(out["requested"]["effort"], "high")
        self.assertEqual(out["route"], "headless")
        self.assertEqual(out["session_id"], SID_CLOSED)

    def test_invalid_values_rejected_before_watch_or_spawn(self):
        for extra in ({"model": ""}, {"model": None}, {"model": "-p"}, {"effort": "ultra"}, {"effort": 1},
                      {"model": "opus", "effort": ""}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    mcp.t_send({"session": "closed chat", "message": "hello", **extra})
                self.assert_nothing_happened()


# --------------------------------------------------------------------------- claude_send, open chat

class SendLive(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(mcp, "resolve", return_value=live_row())
        p.start()
        self.addCleanup(p.stop)

    def test_plain_message_still_delivered(self):
        out = json.loads(mcp.t_send({"session": "live chat", "message": "hello"}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["text"], "hello")
        self.assertEqual(self.spawned, [])
        self.assertEqual(out["route"], "inbox")
        self.assertEqual(self.watched, [SID_LIVE])

    def test_model_override_rejected_before_delivery(self):
        with self.assertRaises(ValueError) as cm:
            mcp.t_send({"session": "live chat", "message": "hello", "model": "opus"})
        msg = str(cm.exception)
        self.assertIn("nothing was sent", msg)
        self.assertIn("'opus'", msg)
        self.assertIn("open, idle", msg)
        self.assertIn("claude_start_session", msg)
        self.assert_nothing_happened()

    def test_effort_override_rejected_before_delivery(self):
        with self.assertRaises(ValueError) as cm:
            mcp.t_send({"session": "live chat", "message": "hello", "effort": "max"})
        self.assertIn("effort 'max'", str(cm.exception))
        self.assert_nothing_happened()

    def test_both_overrides_rejected_before_delivery(self):
        with self.assertRaises(ValueError) as cm:
            mcp.t_send({"session": "live chat", "message": "hello", "model": "fable", "effort": "high"})
        self.assertIn("model 'fable' and effort 'high'", str(cm.exception))
        self.assert_nothing_happened()

    def test_invalid_override_on_live_chat_is_a_validation_error(self):
        with self.assertRaises(ValueError):
            mcp.t_send({"session": "live chat", "message": "hello", "effort": "bogus"})
        self.assert_nothing_happened()

    def test_live_override_does_not_resume_in_parallel(self):
        with self.assertRaises(ValueError):
            mcp.t_send({"session": "live chat", "message": "hello", "model": "opus"})
        self.assertEqual(self.spawned, [])  # no second writer


# --------------------------------------------------------------------------- MCP schema & protocol

def rpc(req: dict) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mcp.handle(req)
    return json.loads(buf.getvalue())


class Schema(unittest.TestCase):
    def tools(self):
        res = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        return {t["name"]: t for t in res}

    def test_model_and_effort_exposed_on_start_and_send(self):
        tools = self.tools()
        for name in ("claude_start_session", "claude_send"):
            props = tools[name]["inputSchema"]["properties"]
            self.assertEqual(props["model"]["type"], "string")
            self.assertEqual(props["effort"]["type"], "string")
            self.assertEqual(props["effort"]["enum"], ["low", "medium", "high", "xhigh", "max"])
            self.assertNotIn("model", tools[name]["inputSchema"]["required"])
            self.assertNotIn("effort", tools[name]["inputSchema"]["required"])

    def test_send_schema_says_closed_chats_only(self):
        props = self.tools()["claude_send"]["inputSchema"]["properties"]
        for key in ("model", "effort"):
            self.assertIn("closed chats only", props[key]["description"])
            self.assertIn("Rejected", props[key]["description"])

    def test_other_tools_untouched(self):
        tools = self.tools()
        for name in ("claude_list_sessions", "claude_read_session", "claude_watch", "claude_wait"):
            self.assertNotIn("model", tools[name]["inputSchema"]["properties"])
            self.assertNotIn("effort", tools[name]["inputSchema"]["properties"])

    def test_schema_is_json_serialisable(self):
        json.dumps(self.tools())

    def test_tools_call_reports_live_override_as_tool_error(self):
        with mock.patch.object(mcp, "resolve", return_value=live_row()), \
                mock.patch.object(mcp, "send_live") as send, mock.patch.object(mcp, "watch") as watch:
            res = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "claude_send",
                                  "arguments": {"session": "live chat", "message": "hi", "model": "opus"}}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("nothing was sent", res["result"]["content"][0]["text"])
        send.assert_not_called()
        watch.assert_not_called()

    def test_tools_call_reports_bad_effort_as_tool_error(self):
        with mock.patch.object(mcp, "spawn_headless") as spawn, mock.patch.object(mcp, "watch") as watch:
            res = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "claude_start_session",
                                  "arguments": {"prompt": "hi", "cwd": _TMP.name, "effort": "ultra"}}})
        self.assertTrue(res["result"]["isError"])
        self.assertIn("ultra", res["result"]["content"][0]["text"])
        spawn.assert_not_called()
        watch.assert_not_called()


class SelectionArgs(unittest.TestCase):
    def test_absent_keys_give_no_flags(self):
        self.assertEqual(mcp.selection_args({"prompt": "x", "permission_mode": "auto"}), ([], {}))

    def test_flag_order_model_then_effort(self):
        flags, req = mcp.selection_args({"effort": "low", "model": "opus"})
        self.assertEqual(flags, ["--model", "opus", "--effort", "low"])
        self.assertEqual(req, {"model": "opus", "effort": "low"})

    def test_null_is_not_treated_as_omitted(self):
        for key in ("model", "effort"):
            with self.assertRaises(ValueError):
                mcp.selection_args({key: None})


if __name__ == "__main__":
    unittest.main()

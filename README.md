# codex-claude-bridge

Lets an OpenAI Codex thread, including Codex live (voice) mode, orchestrate Claude Code chats on the same Mac: list them, read them, message them, start new ones, and get their results back. It uses only the Python standard library.

It has two parts:

- **`claude_sessions_mcp.py`** is an MCP server that Codex loads.
- **`stop_hook.py`** is a Claude Code `Stop` hook that reports each watched chat's final message when a turn ends.

## Tools (as Codex sees them)

| Tool | What it does |
|---|---|
| `claude_list_sessions` | Lists Claude chats, open and closed, with title, id, state (`open, idle` / `open, busy` / `closed`), folder and last activity. Takes filters by text, project or open-only. |
| `claude_read_session` | Shows the last N turns of a chat, with tool activity condensed to one line each. |
| `claude_send` | Messages a chat. **Open chats** get it through Claude Code's cross-session inbox: it starts a turn if the chat is idle, or arrives between tool calls if busy. **Closed chats** are resumed in the background with `claude -p --resume`. The chat is then watched. |
| `claude_start_session` | Starts a new chat in a folder with a first prompt, in the background, and watches it. |
| `claude_watch` | Starts or stops reports from a chat. |
| `claude_wait` | Returns new reports: finished turns (final message), failed background runs, held or refused deliveries, and messages chats sent to the orchestrator. Pass back the returned `cursor`, and keep `timeout_s` short (≤ 30 s) during voice. |

A chat can be referred to by its session id, its desktop-app id (`local_…`), or a unique fragment of its title.

## Install

```bash
# 1. Claude Code Stop hook (add to ~/.claude/settings.json, alongside any existing hooks)
#    {"hooks": {"Stop": [{"hooks": [{"type": "command",
#       "command": "python3 \"$HOME/codex-claude-bridge/stop_hook.py\"", "async": true, "timeout": 10}]}]}}

# 2. Codex MCP server (append to ~/.codex/config.toml, using absolute paths)
#    [mcp_servers.claude_sessions]
#    command = "/usr/local/bin/python3"
#    args = ["/Users/<you>/codex-claude-bridge/claude_sessions_mcp.py"]
#    startup_timeout_sec = 30
#    tool_timeout_sec = 180
#    [mcp_servers.claude_sessions.env]
#    CLAUDE_BIN = "/Users/<you>/.local/bin/claude"

codex mcp list   # should show claude_sessions as enabled
```

Restart the Codex app so new threads load the server.

## Using it from voice mode

Ask, for example: *"List my open Claude chats"*, *"Tell the Claude chat 'monitorability' to rerun the plots with the 64k results"*, or *"Check whether any Claude chats have reported back"*. Voice mode cannot be interrupted by incoming reports, so ask it to check (it calls `claude_wait`) or tell it to keep checking every few minutes while you talk.

## How it behaves, and its limits

- **Reports are pulled, never pushed.** The Codex desktop app gives outside programs no way to start a turn in its threads, so the orchestrator has to call `claude_wait`.
- **One writer per chat.** An open chat is only ever messaged through its inbox, never resumed in parallel, since a second process would interleave into the same transcript.
- **Messages from Codex arrive as messages from another session, not from you.** Claude acts on them within that chat's own permissions. They can't approve prompts or change settings. Background runs for closed chats use `--permission-mode auto` unless you pass another mode.
- **Only watched chats are reported.** The hook ignores every other chat. State lives in `~/.claude-bridge/`: `watched.json`, `events.jsonl` (rotated at 20 MB), and background job results in `jobs/`.
- **New chats started from Codex run in the background.** They don't appear in the Claude desktop app's sidebar.
- **It relies on undocumented parts of Claude Code:** the session registry in `~/.claude/sessions/` and the inbox message format. It was tested against Claude Code 2.1.280–2.1.281 and Codex 0.153 / 0.155-alpha on macOS.

## Comparison with other tools

This compares only the job this repo does: a Codex thread, ideally Codex desktop's live (voice) mode, driving your Claude Code chats and hearing back from them. Several tools below do more in other directions. Research depth varies:

- **antiphon:** source read.
- **Rows marked \*:** READMEs read.
- **Grouped rows:** judged from repo descriptions only.

Everything was checked in September 2026.

| Tool | Usable from Codex desktop / live mode | Messages your **existing, open** Claude chats | Messages **closed** chats | Reads chat **history** | Starts new chats | Results back to the orchestrator | Reporting doesn't rely on the model | License · maturity |
|---|---|---|---|---|---|---|---|---|
| **codex-claude-bridge** (this) | ✅ MCP | ✅ native inbox | ✅ background resume | ✅ | ✅ background | ✅ pull (`claude_wait`) | ✅ Stop hook | none yet · new, tested on this Mac |
| [antiphon](https://github.com/moiri-gamboni/antiphon) | ⚠️ via its CLI; desktop threads can't be peers, since the desktop app runs a private Codex engine | ✅ native inbox | ❌ | ❌ | ✅ with approvals forwarded to Codex | ✅ push to Codex threads on the shared daemon (not desktop) · `wait` for its own sessions | ✅ | AGPL-3.0 · young, 0 stars |
| [claude-code-messaging](https://github.com/samsonjs/claude-code-messaging)\* | ✅ Codex skill (shell) | ✅ native inbox | ❌ | ❌ | ❌ | ✅ pull from a persistent reply inbox | ❌ depends on the chat replying | none · 1 commit, 0 stars |
| [Orchestra-MCP](https://github.com/ngoclinh93qt/Orchestra-MCP)\* | ✅ MCP | ❌ runs its own tasks only | ❌ | ✅ Claude **and** Codex history | ✅ supervised tasks | ✅ pull (`agent_output`) | ✅ | all rights reserved · 1 star |
| [claude-agents-mcp](https://github.com/kroticw/claude-agents-mcp)\* | ✅ MCP | ❌ `claude agents` background sessions only | ⚠️ its own sessions | ⚠️ reads the terminal screen | ✅ | ⚠️ inter-agent `send_message` | ❌ | Apache-2.0 · inactive fork |
| [claw-orchestrator](https://github.com/Enderfga/claw-orchestrator)\* | ✅ MCP (78 tools) | ❌ its own persistent sessions only | ❌ | ⚠️ its own sessions | ✅ | ✅ run ledger, dashboard, polling | ✅ | MIT · most mature (≈580 stars) |
| [orchestrator-mcp](https://github.com/crAK1644/orchestrator-mcp)\* | ✅ MCP | ❌ its own consultations only | ⚠️ resumes its own by id | ❌ | ✅ | ✅ synchronous answer | ✅ | MIT · 10 stars |
| [claude-code-mcp](https://github.com/steipete/claude-code-mcp) | ✅ MCP | ❌ one-shot sessions | ❌ | ❌ | ✅ one-shot | ✅ synchronous answer | ✅ | MIT · established |
| MCP message relays¹ | ✅ MCP | ⚠️ only chats that also load the relay and check it | ❌ | ❌ | ❌ | ⚠️ inbox the model must check | ❌ | various · small |
| Channels bridges² | ❌ Codex CLI / app-server | ⚠️ one paired session | ❌ | ❌ | ❌ | ✅ push | ⚠️ | various · small |
| History search MCPs³ | ✅ MCP | ❌ read-only | ❌ | ✅ search | ❌ | – | – | various |
| [herdr-voice-mcp](https://github.com/lorenzkromer/herdr-voice-mcp)\* | ❌ the voice client is the **Claude** app, not Codex | ⚠️ only agents running inside Herdr's terminal multiplexer | ❌ | ⚠️ recent terminal lines | ✅ | ✅ **push notifications** to your phone | ✅ | MIT · early, 0 stars |

¹ [cross-agent_mcp](https://github.com/whooperlove/cross-agent_mcp), [agent-peers-mcp](https://github.com/Co-Messi/agent-peers-mcp), [agent-comms-mcp](https://github.com/watchout/agent-comms-mcp).
² [agent-bridge](https://github.com/raysonmeng/agent-bridge), [codex-claude-bridge](https://github.com/abhishekgahlot2/codex-claude-bridge), [agent-chat](https://github.com/HJewkes/agent-chat).
³ [cc-session-search](https://github.com/bwads001/cc-session-search), [ClaudeHistoryMCP](https://github.com/jhammant/ClaudeHistoryMCP), [claude-conversation-search-mcp](https://github.com/ticpu/claude-conversation-search-mcp).

"–" means not applicable or not determined.

**In short:**
- **This repo** is the only one that combines your existing Claude desktop chats, open **or** closed, with Codex desktop's live mode and hook-based reporting.
- **antiphon** is the better fit if the orchestrator is a terminal `codex` session: it gives both directions, push reports and forwarded approvals.
- **claw-orchestrator** is the most mature choice if you're happy for the orchestrator to run its own fresh sessions rather than your existing chats.
- **herdr-voice-mcp** takes the opposite approach: voice through the Claude app, with push notifications.

## Tested

- A new chat was started from the MCP server and its reply reported via the hook.
- A closed chat was messaged, resumed in the background, and its reply reported.
- A live background chat was messaged through its inbox, it replied, and the reply was reported.
- End to end from `codex exec`: Codex listed the chat, sent a message, waited, and relayed the Claude chat's exact reply.
- Not yet tested from voice mode itself: that needs a person speaking.

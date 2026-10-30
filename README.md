# Telegram ⇄ Claude Code bridge

Text a Telegram bot from your phone. A real Claude Code session runs on this
desktop — your `CLAUDE.md`, hooks, skills, MCP servers and full tool access —
and the answer comes back as a Telegram message.

No dependencies beyond Python 3.10+. Nothing to `pip install`.

**Setting this up for the first time?** There is a longer illustrated guide in
[`docs/index.html`](docs/index.html) — written to be published as a GitHub Page —
covering setup, configuration, and the memory system that keeps context across
sessions.

---

## Quick start

**1. Make a bot.** In Telegram, message [@BotFather](https://t.me/BotFather),
send `/newbot`, follow the prompts, and copy the token it gives you.

**2. Run setup.**

```
run-bridge.bat
```

With no `config.json` present it runs `python bridge.py --setup`, which asks for
the token, then waits for you to message the bot so it can learn your Telegram
user id and write both `.env` and `config.json`.

**3. Message your bot.** Anything you send is a prompt.

**4. (Optional) Start it automatically at logon.**

```powershell
powershell -ExecutionPolicy Bypass -File install-startup.ps1
```

Everything is logged to `bridge.log` in this folder.

---

## Commands

| Command | What it does |
| --- | --- |
| *(anything else)* | A prompt, sent to Claude |
| `/new` | Forget the conversation, start fresh |
| `/stop` | Abort the turn that's running |
| `/status` | Working dir, model, session, spend |
| `/cd <path>` | Change working directory (starts a fresh conversation there) |
| `/model <name>` | e.g. `opus`, `sonnet`, `fable` — applies to your next message |
| `/send <path>` | Send a file from the desktop back to you |
| `/help` | The command list |

Send a photo or a document and it's saved to `inbox/`; the local path is handed
to Claude in the prompt, so you can say *"what's wrong with this screenshot?"*.

While a turn runs you get a live status message listing the tools being used,
updated in place, plus a `<n> tool calls · 42s · $0.031` footer when it finishes.

---

## How it works

The obvious implementation — spawn `claude -p "<message>"` per message — was
measured at **~91k uncached input tokens (~$0.46) for a one-word reply**, because
every message re-primes the whole system prompt, tool definitions and MCP tool
list from scratch.

So instead the bridge keeps **one long-lived `claude` process per chat**, spoken
to over the stream-json protocol:

```
claude -p --input-format stream-json --output-format stream-json --verbose
```

User turns are written to its stdin as newline-delimited JSON; `assistant`,
`system` and `result` events come back on stdout. A `result` event ends the turn.

The payoff, measured on this machine across two turns of the same conversation:

| | input tokens | cached read |
| --- | --- | --- |
| turn 1 | 57,868 | 53,376 |
| turn 2 | **170** | 44,416 |

Conversation memory is the child process's own session. If it dies, the bridge
restarts it with `--resume <session_id>`, so the thread of the conversation
survives a crash. Idle sessions are shut down after `idle_session_timeout_sec`
(default 30 min) and resume on your next message.

**Layout**

| File | Role |
| --- | --- |
| `bridge.py` | Poll loop, per-chat worker, commands, setup/check CLI |
| `claude_session.py` | The long-lived `claude` process and its JSON protocol |
| `tg.py` | Telegram Bot API client — splitting, HTML, uploads, retries |
| `selftest.py` | 47 offline checks (`python selftest.py`) |
| `run-bridge.bat` | Launcher, with crash-restart |
| `install-startup.ps1` | Optional logon task |

---

## Cost

This is not free and not trivial — it is running a full agent, with your MCP
servers attached. Measured here: **~$0.02–$0.35 per turn** depending on how much
work Claude does and how well the prompt cache hits.

Set `max_budget_usd_per_turn` in `config.json` (e.g. `2.0`) for a hard per-turn
cap. `null` means uncapped. `/status` shows cumulative session spend.

---

## Security — read this

`skip_permissions` is `true` by default. That is what "in full force" requires:
a headless run cannot answer a permission prompt, so anything needing approval
would otherwise just fail. The consequence is real:

> **Anyone who can message this bot can run commands on this desktop as you,
> with no confirmation.** Any prompt asking Claude to delete files, push a repo,
> or send email will simply be carried out.

The protections that are actually in place:

- **`allowed_user_ids`** — the bridge refuses every message from anyone not on
  the list, and refuses to start at all if the list is empty. It replies with
  the sender's Telegram id so you can add a legitimate second device. Do not
  put an id you don't recognise into that list.
- **`allowed_roots`** — `/cd` and `/send` are confined to these directories.
- Your bot token is the only credential; it lives in `.env`, which is
  gitignored. **Anyone with the token controls the bot** — treat it like a
  password. If it leaks, `/revoke` it with @BotFather.
- Telegram bots are discoverable by username. An unguessable bot name is mild
  obscurity, not security; `allowed_user_ids` is the actual gate.

To run read-only instead, set `skip_permissions: false`. Prompts are then
auto-denied rather than hanging — so anything needing approval fails cleanly
instead of silently running. It's a blunt instrument, but it's a real boundary.

---

## Troubleshooting

**"Not authorized" and no reply from Claude.** By design. Add the user id it
prints to `allowed_user_ids` in `config.json` and restart.

**Nothing happens when you message it.** Check `bridge.log`. If the process
isn't running, `run-bridge.bat` will tell you why.

**It replied but the formatting is odd.** Telegram's HTML mode rejects a whole
message if any entity is malformed; the client already retries as plain text
when that happens. If you see raw `**asterisks**`, that fallback fired.

**Claude exited unexpectedly.** It auto-resumes with `--resume`, so just send
your next message. Repeated exits mean a broken hook or MCP server — check
`bridge.log`, which keeps the child's last stderr lines.

**Costs.** See above. Lower `idle_session_timeout_sec` if sessions linger, and
consider `/model sonnet` for routine work.

---

## Files not to commit

`.gitignore` already covers `config.json`, `.env`, `state.json`, `bridge.log`
and `inbox/` — the token and everything Claude touched. Keep it that way.

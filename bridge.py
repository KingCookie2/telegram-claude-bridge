"""Telegram <-> Claude Code bridge.

Text your Telegram bot; a full Claude Code session runs on this desktop with
your MCP servers, hooks, CLAUDE.md and tools, and texts the answer back.

    python bridge.py            run the bot
    python bridge.py --setup    one-time interactive setup
    python bridge.py --check    verify config without running

Design notes
------------
* One long-lived `claude` child process per chat, spoken to over stream-json
  (see claude_session.py). Conversation memory is the child's own session, so
  follow-up messages are cheap instead of re-priming ~90k tokens each time.
* The Telegram poll loop owns the main thread; each chat gets a worker thread.
  Commands that need the session to restart (/new, /cd, /model) act immediately
  and kill the child -- the worker notices via on_exit and unblocks.
* Read-only by convention for everyone else: an allowlist of Telegram user ids
  is enforced before anything reaches Claude. See README "Security".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

from claude_session import ClaudeSession, describe_tool, find_claude_command
from tg import Telegram, TelegramError

log = logging.getLogger("bridge")

HELP_TEXT = """\
Send me any message and I'll run it through Claude Code on your desktop.

*Commands*
/new — start a fresh conversation
/stop — abort the turn that's running
/status — session, working dir, spend
/cd <path> — change working directory
/model <name> — e.g. opus, sonnet, fable
/send <path> — send a file from the desktop back to you
/help — this message

Anything else is a prompt. Send a photo or file and it gets saved locally,
with the path handed to Claude.
"""

DEFAULT_SYSTEM_PROMPT = """\
You are replying to your user over Telegram, running on their own desktop
machine. They are almost certainly on a phone.

- Your final message is the ONLY thing they see. Tool calls, tool output and
  intermediate notes are not shown to them. Never end a turn with just
  "let me know if you'd like me to..." — either do the work or ask one
  specific question.
- Keep it short and phone-readable: a few sentences, or a short list. Avoid
  large code blocks, file dumps and wide tables unless explicitly asked.
- You are running unattended and cannot ask a clarifying question mid-task.
  Make a sensible assumption, do the work, and say what you assumed. If a
  decision genuinely blocks you, end with that one question.
- You have full tool permissions. Do the work rather than explaining how.
- If you changed files, name them and say what changed, in a line or two.
"""

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "allowed_user_ids": [],
    "allowed_chat_ids": [],
    "default_cwd": "",
    "allowed_roots": [],
    "model": "",
    "skip_permissions": True,
    "permission_mode": "bypassPermissions",
    "setting_sources": "",
    "max_budget_usd_per_turn": None,
    "turn_timeout_sec": 3600,
    "include_partial_messages": False,
    "extra_dirs": [],
    "append_system_prompt": DEFAULT_SYSTEM_PROMPT,
    "claude_command": "",
    "progress_updates": True,
    "progress_edit_interval_sec": 3.0,
    "max_queue": 5,
    "idle_session_timeout_sec": 1800,
    "max_upload_mb": 45,
}


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines into os.environ without clobbering real env vars."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env_token:
        cfg["telegram_bot_token"] = env_token
    return cfg


def save_json_atomic(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


class State:
    """Small persistent blob: poll offset plus per-chat cwd and session id."""

    def __init__(self, path: str):
        self.path = path
        self.offset: int | None = None
        self.chats: dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.offset = data.get("offset")
                self.chats = data.get("chats") or {}
            except Exception as exc:
                log.warning("could not read %s (%s); starting fresh", path, exc)

    def chat(self, chat_id) -> dict:
        return self.chats.setdefault(str(chat_id), {})

    def save(self) -> None:
        try:
            save_json_atomic(self.path, {"offset": self.offset, "chats": self.chats})
        except Exception as exc:
            log.warning("could not save state: %s", exc)


def setup_logging(logfile: str | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if logfile:
        handler = RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

@dataclass
class Job:
    kind: str                       # "prompt"
    text: str = ""
    message_id: int | None = None
    attachments: list[str] = field(default_factory=list)


def is_under(path: str, roots: list[str]) -> bool:
    target = os.path.normcase(os.path.abspath(path))
    for root in roots:
        root_n = os.path.normcase(os.path.abspath(root))
        if target == root_n or target.startswith(root_n + os.sep):
            return True
    return False


# --------------------------------------------------------------------------
# per-chat worker
# --------------------------------------------------------------------------

class ChatWorker(threading.Thread):
    def __init__(self, bridge: "Bridge", chat_id: int):
        super().__init__(daemon=True, name=f"chat-{chat_id}")
        self.bridge = bridge
        self.cfg = bridge.cfg
        self.tg = bridge.tg
        self.chat_id = chat_id

        saved = bridge.state.chat(chat_id)
        self.cwd = saved.get("cwd") or bridge.default_cwd
        self.model = self.cfg.get("model") or None
        self.session: ClaudeSession | None = None

        self.queue: queue.Queue[Job | None] = queue.Queue()
        self.last_activity = time.time()

        self._turn_done = threading.Event()
        self._turn_result: dict | None = None
        self._awaiting_turn = False
        self._stop_requested = False

        self._progress_lines: list[str] = []
        self._progress_msg_id: int | None = None
        self._progress_started = 0.0
        self._last_progress_edit = 0.0
        self._busy_tools = 0

    # -- public API (called from the poll thread) -------------------------

    def submit(self, job: Job) -> bool:
        if self.queue.qsize() >= self.cfg["max_queue"]:
            return False
        self.queue.put(job)
        return True

    def interrupt(self, reason: str = "🛑 Stopped.") -> bool:
        """Kill the running turn. Returns True if something was running."""
        if not self._awaiting_turn:
            return False
        self._stop_requested = True
        self._turn_result = {"is_error": True, "result": reason}
        if self.session:
            self.session.interrupt()
        self._turn_done.set()
        return True

    def reset_session(self) -> None:
        """Forget the conversation entirely (/new, /cd)."""
        if self.session:
            self.session.stop()
            self.session.session_id = None
        self.bridge.state.chat(self.chat_id).pop("session_id", None)

    def status(self) -> dict:
        sess = self.session
        return {
            "cwd": self.cwd,
            "model": self.model or "(default)",
            "session_id": (sess.session_id if sess else None)
                          or self.bridge.state.chat(self.chat_id).get("session_id"),
            "alive": bool(sess and sess.alive),
            "busy": self._awaiting_turn,
            "queued": self.queue.qsize(),
            "turns": sess.turn_count if sess else 0,
            "cost": sess.total_cost_usd if sess else 0.0,
            "idle_sec": int(time.time() - self.last_activity),
        }

    # -- worker loop ------------------------------------------------------

    def run(self) -> None:
        while self.bridge.running:
            try:
                job = self.queue.get(timeout=0.5)
            except queue.Empty:
                self._maybe_reap()
                continue
            if job is None:
                break

            self.last_activity = time.time()
            try:
                self._run_prompt(job)
            except Exception:
                log.error("worker error:\n%s", traceback.format_exc())
                self._safe_send("❌ Internal bridge error:\n```\n"
                                + traceback.format_exc(limit=3)[-900:] + "\n```")

        if self.session:
            self.session.stop()

    def _run_prompt(self, job: Job) -> None:
        if not self._ensure_session():
            return

        text = job.text
        if job.attachments:
            listing = "\n".join(f"- {p}" for p in job.attachments)
            text = f"{text}\n\n[Attached file(s), saved on the desktop:]\n{listing}"

        self._stop_requested = False
        self._turn_done.clear()
        self._turn_result = None
        self._progress_lines = []
        self._progress_started = time.time()
        self._last_progress_edit = 0.0
        self._busy_tools = 0

        if self.cfg["progress_updates"]:
            preview = " ".join(job.text.split())[:80] or "(attachment)"
            self._progress_msg_id = self._post_progress(f"⏳ {preview}")

        # The CLI reports cost cumulatively for the session, so a single turn's
        # spend is the delta across it.
        cost_before = self.session.total_cost_usd if self.session else 0.0

        # Set before the heartbeat starts: _heartbeat loops on this flag, so
        # starting it first would let it see False and exit without ever
        # sending a typing action.
        self._awaiting_turn = True
        try:
            assert self.session
            self.session.send(text)
        except Exception as exc:
            self._awaiting_turn = False
            self._finish_progress(f"❌ Could not reach Claude: {exc}")
            return

        threading.Thread(target=self._heartbeat, daemon=True).start()

        timeout = float(self.cfg["turn_timeout_sec"])
        deadline = time.time() + timeout

        while not self._turn_done.wait(1.0):
            if not self.bridge.running:
                return
            if time.time() > deadline:
                self._safe_send(
                    f"⏱ Still working after {int(timeout / 60)} min. "
                    "Send /stop to abort, or /status for a look."
                )
                deadline = time.time() + timeout

        self._awaiting_turn = False
        result = self._turn_result or {}
        elapsed = time.time() - self._progress_started
        cost = max(0.0, (self.session.total_cost_usd if self.session else 0.0)
                   - cost_before)

        reply = ClaudeSession.result_text(result)
        errored = bool(result.get("is_error"))

        if errored:
            detail = result.get("result") or "Claude reported an error."
            footer = f"⚠️ {elapsed:.0f}s"
            if not reply:
                reply = f"⚠️ {detail}"
            self._finish_progress(footer)
        elif not reply:
            reply = "✅ Done (no text output)."
            self._finish_progress(f"✅ {elapsed:.0f}s")
        else:
            extras = [f"{elapsed:.0f}s"]
            if cost:
                extras.append(f"${cost:.4f}")
            if self._busy_tools:
                extras.append(f"{self._busy_tools} tool calls")
            self._finish_progress("✅ " + " · ".join(extras))

        self._safe_send_markdown(reply, reply_to=job.message_id)

        self.bridge.state.chat(self.chat_id)["session_id"] = (
            self.session.session_id if self.session else None
        )
        self.bridge.state.save()

    # -- session ----------------------------------------------------------

    def _ensure_session(self) -> bool:
        if self.session and self.session.alive:
            return True

        try:
            command = find_claude_command(self.cfg.get("claude_command", ""))
        except FileNotFoundError as exc:
            self._safe_send(f"❌ {exc}")
            return False

        extra_dirs = list(self.cfg.get("extra_dirs") or [])
        if self.bridge.inbox_dir not in extra_dirs:
            extra_dirs.append(self.bridge.inbox_dir)

        saved_sid = self.bridge.state.chat(self.chat_id).get("session_id")
        if self.session is None:
            self.session = ClaudeSession(
                command=command,
                cwd=self.cwd,
                model=self.model,
                permission_mode=self.cfg["permission_mode"],
                skip_permissions=bool(self.cfg["skip_permissions"]),
                max_budget_usd=self.cfg.get("max_budget_usd_per_turn"),
                append_system_prompt=self.cfg.get("append_system_prompt") or None,
                extra_dirs=tuple(extra_dirs),
                include_partial=bool(self.cfg.get("include_partial_messages")),
                setting_sources=self.cfg.get("setting_sources") or None,
                resume_session_id=saved_sid,
                on_event=self._on_event,
                on_turn_end=self._on_turn_end,
                on_exit=self._on_exit,
                on_log=log.debug,
            )
        else:
            if saved_sid and not self.session.session_id:
                self.session.session_id = saved_sid
            self.session.cwd = self.cwd
            self.session.model = self.model

        try:
            self.session.start()
        except Exception as exc:
            self._safe_send(f"❌ Could not start Claude Code: {exc}")
            return False
        return True

    def _maybe_reap(self) -> None:
        idle_limit = float(self.cfg["idle_session_timeout_sec"])
        if (self.session and self.session.alive and not self._awaiting_turn
                and time.time() - self.last_activity > idle_limit):
            log.info("chat %s idle %.0fs, stopping claude process", self.chat_id, idle_limit)
            self.bridge.state.chat(self.chat_id)["session_id"] = self.session.session_id
            self.bridge.state.save()
            self.session.stop()

    # -- claude callbacks (reader thread) ---------------------------------

    def _on_event(self, kind: str, payload: dict) -> None:
        if kind == "assistant":
            _texts, tools = ClaudeSession.extract_blocks(payload.get("message") or {})
            for tool in tools:
                self._busy_tools += 1
                self._progress_lines.append("🔧 " + describe_tool(tool))

    def _on_turn_end(self, result: dict) -> None:
        self._turn_result = result
        self._turn_done.set()

    def _on_exit(self, code: int, expected: bool) -> None:
        if self._awaiting_turn and not self._turn_done.is_set():
            if self._stop_requested:
                self._turn_result = {"is_error": True, "result": "🛑 Stopped."}
            else:
                self._turn_result = {
                    "is_error": True,
                    "result": (f"⚠️ Claude exited unexpectedly (rc={code}). "
                               "Send another message and I'll resume the conversation."),
                }
            self._turn_done.set()
        elif not expected and not self._awaiting_turn:
            log.warning("chat %s: claude exited rc=%s while idle", self.chat_id, code)

    # -- progress message -------------------------------------------------

    def _progress_text(self, header: str) -> str:
        lines = self._progress_lines[-12:]
        body = "\n".join(lines)
        return f"{header}\n{body}" if body else header

    def _post_progress(self, header: str) -> int | None:
        try:
            msg = self.tg.send_message(self.chat_id, self._progress_text(header))
            return msg["message_id"]
        except TelegramError as exc:
            log.warning("could not post progress message: %s", exc)
            return None

    def _touch_progress(self) -> None:
        """Throttled edit of the live progress message."""
        if not self._progress_msg_id or not self.cfg["progress_updates"]:
            return
        interval = float(self.cfg["progress_edit_interval_sec"])
        now = time.time()
        if now - self._last_progress_edit < interval:
            return
        self._last_progress_edit = now
        header = f"⏳ {now - self._progress_started:.0f}s · {self._busy_tools} tool calls"
        try:
            self.tg.edit_message_text(self.chat_id, self._progress_msg_id,
                                      self._progress_text(header))
        except TelegramError:
            pass  # not-modified and rate limits are both fine to ignore

    def _finish_progress(self, footer: str) -> None:
        if not self._progress_msg_id:
            return
        try:
            self.tg.edit_message_text(self.chat_id, self._progress_msg_id, footer)
        except TelegramError:
            pass
        self._progress_msg_id = None

    def _heartbeat(self) -> None:
        while self._awaiting_turn and self.bridge.running:
            self.tg.send_chat_action(self.chat_id, "typing")
            self._touch_progress()
            time.sleep(4.0)

    # -- sending ----------------------------------------------------------

    def _safe_send(self, text: str) -> None:
        try:
            self.tg.send_message(self.chat_id, text)
        except TelegramError as exc:
            log.warning("send failed: %s", exc)

    def _safe_send_markdown(self, text: str, reply_to: int | None = None) -> None:
        try:
            self.tg.send_markdown(self.chat_id, text, reply_to=reply_to)
        except TelegramError as exc:
            log.warning("markdown send failed (%s); retrying as plain text", exc)
            try:
                self.tg.send_message(self.chat_id, text, reply_to=reply_to)
            except TelegramError as exc2:
                log.error("plain send failed too: %s", exc2)


# --------------------------------------------------------------------------
# bridge
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg: dict, root_dir: str):
        self.cfg = cfg
        self.root_dir = root_dir
        self.tg = Telegram(cfg["telegram_bot_token"])
        self.state = State(os.path.join(root_dir, "state.json"))
        self.inbox_dir = os.path.join(root_dir, "inbox")
        self.allowed_users = {int(x) for x in cfg.get("allowed_user_ids") or []}
        self.allowed_chats = {int(x) for x in cfg.get("allowed_chat_ids") or []}
        self.roots = [r for r in (cfg.get("allowed_roots") or []) if r]
        self.default_cwd = cfg.get("default_cwd") or (self.roots[0] if self.roots else root_dir)
        self.workers: dict[int, ChatWorker] = {}
        self.workers_lock = threading.Lock()
        self.running = True

    # -- helpers ----------------------------------------------------------

    def worker_for(self, chat_id: int) -> ChatWorker:
        with self.workers_lock:
            worker = self.workers.get(chat_id)
            if worker is None or not worker.is_alive():
                worker = ChatWorker(self, chat_id)
                worker.start()
                self.workers[chat_id] = worker
            return worker

    def authorized(self, message: dict) -> bool:
        if not self.allowed_users and not self.allowed_chats:
            return False
        sender = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        return (sender in self.allowed_users) or (chat_id in self.allowed_chats)

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        me = self.tg.get_me()
        log.info("connected as @%s (id %s)", me.get("username"), me.get("id"))
        log.info("working dir: %s", self.default_cwd)
        log.info("allowed user ids: %s", sorted(self.allowed_users) or "NONE (locked)")
        self._drain_and_report()

        while self.running:
            try:
                updates = self.tg.get_updates(offset=self.state.offset, timeout=50)
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                time.sleep(3)
                continue
            except KeyboardInterrupt:
                break

            for update in updates:
                self.state.offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:
                    log.error("update handler error:\n%s", traceback.format_exc())
            if updates:
                self.state.save()

    def _drain_and_report(self) -> None:
        """Skip anything that queued up while the bridge was down."""
        try:
            pending = self.tg.get_updates(offset=self.state.offset, timeout=0)
        except TelegramError:
            return
        stale = 0
        for update in pending:
            self.state.offset = update["update_id"] + 1
            message = update.get("message") or {}
            if time.time() - (message.get("date") or 0) > 120:
                stale += 1
        if stale:
            log.info("skipped %d message(s) received while offline", stale)
        self.state.save()

    # -- routing ----------------------------------------------------------

    def handle(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        if not message:
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        sender = message.get("from") or {}
        if chat_id is None or sender.get("is_bot"):
            return

        if not self.authorized(message):
            log.warning("rejected message from user id %s (%s), chat %s",
                        sender.get("id"), sender.get("first_name"), chat_id)
            self._reject(chat_id, sender)
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        attachments = self._collect_attachments(message)

        if text.startswith("/"):
            self._command(chat_id, text, message, attachments)
            return

        if not text and not attachments:
            return

        self._enqueue(chat_id, text or "(see attached file)", message, attachments)

    def _reject(self, chat_id: int, sender: dict) -> None:
        note = (f"Not authorized.\nYour Telegram user id is {sender.get('id')}.\n"
                f"Add it to allowed_user_ids in config.json to use this bot.")
        if self.allowed_users or self.allowed_chats:
            note = f"Not authorized.\nYour Telegram user id is {sender.get('id')}."
        try:
            self.tg.send_message(chat_id, note)
        except TelegramError:
            pass

    def _enqueue(self, chat_id: int, text: str, message: dict,
                 attachments: list[str]) -> None:
        worker = self.worker_for(chat_id)
        job = Job(kind="prompt", text=text,
                  message_id=message.get("message_id"), attachments=attachments)
        if not worker.submit(job):
            self.tg.send_message(chat_id, "Queue is full — wait for the current turn.")
            return
        if worker.status()["busy"] or worker.queue.qsize() > 1:
            self.tg.send_chat_action(chat_id, "typing")

    def _collect_attachments(self, message: dict) -> list[str]:
        file_id = None
        name = None

        if message.get("photo"):
            file_id = message["photo"][-1]["file_id"]
            name = "photo.jpg"
        elif message.get("document"):
            file_id = message["document"]["file_id"]
            name = message["document"].get("file_name")
        elif message.get("voice"):
            file_id = message["voice"]["file_id"]
            name = "voice.ogg"
        elif message.get("audio"):
            file_id = message["audio"]["file_id"]
            name = message["audio"].get("file_name") or "audio.mp3"
        elif message.get("video"):
            file_id = message["video"]["file_id"]
            name = message["video"].get("file_name") or "video.mp4"

        if not file_id:
            return []
        try:
            # Telegram names the local copy from its own file_path; `name` above
            # is only used to tell the user what kind of thing arrived.
            path = self.tg.download_file(file_id, self.inbox_dir)
            log.info("saved attachment (%s) to %s", name or "file", path)
            return [path]
        except TelegramError as exc:
            log.warning("attachment download failed: %s", exc)
            return []

    # -- commands ---------------------------------------------------------

    def _command(self, chat_id: int, text: str, message: dict,
                 attachments: list[str]) -> None:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = " ".join(parts[1:]).strip()

        if cmd in ("/start", "/help"):
            self.tg.send_markdown(chat_id, HELP_TEXT)
            return

        if cmd == "/status":
            worker = self.worker_for(chat_id)
            st = worker.status()
            sid = (st["session_id"] or "—")[:8]
            self.tg.send_message(
                chat_id,
                f"cwd: {st['cwd']}\n"
                f"model: {st['model']}\n"
                f"session: {sid}\n"
                f"claude running: {'yes' if st['alive'] else 'no'}\n"
                f"busy: {'yes' if st['busy'] else 'no'}   queued: {st['queued']}\n"
                f"turns: {st['turns']}   spend: ${st['cost']:.4f}",
            )
            return

        if cmd == "/stop":
            worker = self.worker_for(chat_id)
            if worker.interrupt():
                self.tg.send_message(chat_id, "🛑 Stopping…")
            else:
                self.tg.send_message(chat_id, "Nothing is running.")
            return

        if cmd == "/new":
            worker = self.worker_for(chat_id)
            worker.reset_session()
            self.tg.send_message(chat_id, "🧹 Fresh conversation.")
            return

        if cmd == "/cd":
            if not arg:
                self.tg.send_message(chat_id, "Usage: /cd <path>")
                return
            target = os.path.abspath(os.path.expanduser(arg))
            if not os.path.isdir(target):
                self.tg.send_message(chat_id, f"Not a directory: {target}")
                return
            if self.roots and not is_under(target, self.roots):
                self.tg.send_message(
                    chat_id,
                    "Outside allowed_roots:\n" + "\n".join(self.roots))
                return
            worker = self.worker_for(chat_id)
            worker.interrupt("🛑 Stopped (changing directory).")
            worker.cwd = target
            worker.reset_session()
            self.state.chat(chat_id)["cwd"] = target
            self.state.save()
            self.tg.send_message(chat_id, f"📁 {target}\n(started a fresh conversation here)")
            return

        if cmd == "/model":
            worker = self.worker_for(chat_id)
            if not arg:
                self.tg.send_message(chat_id, f"model: {worker.model or '(default)'}")
                return
            worker.interrupt("🛑 Stopped (changing model).")
            worker.model = arg
            if worker.session:
                worker.session.model = arg
            self.tg.send_message(chat_id, f"🤖 {arg} (applies to your next message)")
            return

        if cmd == "/send":
            self._send_file(chat_id, arg)
            return

        self.tg.send_message(chat_id, f"Unknown command {cmd}. Try /help")

    def _send_file(self, chat_id: int, arg: str) -> None:
        if not arg:
            self.tg.send_message(chat_id, "Usage: /send <path>")
            return
        path = os.path.abspath(os.path.expanduser(arg))
        if not os.path.isfile(path):
            self.tg.send_message(chat_id, f"No such file: {path}")
            return
        if self.roots and not is_under(path, self.roots):
            self.tg.send_message(chat_id, "Refusing: outside allowed_roots.")
            return
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > float(self.cfg["max_upload_mb"]):
            self.tg.send_message(
                chat_id, f"Too big: {size_mb:.1f} MB (limit {self.cfg['max_upload_mb']} MB)")
            return
        try:
            self.tg.send_document(chat_id, path)
        except TelegramError as exc:
            self.tg.send_message(chat_id, f"Upload failed: {exc}")


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def cmd_check(cfg: dict, root_dir: str) -> int:
    ok = True

    token = cfg.get("telegram_bot_token", "")
    if not token or ":" not in token:
        print("[ERROR] no bot token. Put TELEGRAM_BOT_TOKEN in .env, or run --setup.")
        ok = False
    else:
        try:
            tg = Telegram(token)
            me = tg.get_me()
            print(f"[ok]    telegram: @{me.get('username')} (id {me.get('id')})")
        except Exception as exc:
            print(f"[ERROR] telegram: {exc}")
            ok = False

    try:
        command = find_claude_command(cfg.get("claude_command", ""))
        print(f"[ok]    claude: {command}")
    except FileNotFoundError as exc:
        print(f"[ERROR] claude: {exc}")
        ok = False

    cwd = cfg.get("default_cwd") or root_dir
    if os.path.isdir(cwd):
        print(f"[ok]    working dir: {cwd}")
    else:
        print(f"[ERROR] working dir does not exist: {cwd}")
        ok = False

    users = cfg.get("allowed_user_ids") or []
    if users:
        print(f"[ok]    allowed users: {users}")
    else:
        print("[WARN]  allowed_user_ids is empty — the bot will reject every message.")
        print("        Run --setup, or add your Telegram user id to config.json.")

    mode = "bypassPermissions (unattended, full tool access)" if cfg.get("skip_permissions") \
        else f"{cfg.get('permission_mode')} (prompts auto-denied)"
    print(f"[ok]    permission mode: {mode}")

    if not cfg.get("max_budget_usd_per_turn"):
        print("[WARN]  max_budget_usd_per_turn is null — no per-turn spend cap.")
    return 0 if ok else 1


def cmd_setup(cfg_path: str, root_dir: str) -> int:
    print("Telegram <-> Claude Code bridge setup")
    print("-" * 40)
    print("You need a bot token. In Telegram, message @BotFather:")
    print("  /newbot  ->  pick a name  ->  copy the token it gives you\n")

    try:
        token = input("Bot token: ").strip()
    except EOFError:
        print("No input available; re-run this in an interactive terminal.")
        return 1
    if not token:
        print("No token given.")
        return 1

    tg = Telegram(token)
    try:
        me = tg.get_me()
    except Exception as exc:
        print(f"[ERROR] that token did not work: {exc}")
        return 1
    print(f"\n[ok] connected as @{me.get('username')}")

    env_path = os.path.join(root_dir, ".env")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write(f"TELEGRAM_BOT_TOKEN={token}\n")
    print(f"[ok] wrote {env_path}")

    print("\nNow send your bot any message from Telegram (say 'hi').")
    print("Waiting up to 3 minutes to learn your user id...\n")

    offset, user = None, None
    deadline = time.time() + 180
    while time.time() < deadline and not user:
        try:
            for update in tg.get_updates(offset=offset, timeout=10):
                offset = update["update_id"] + 1
                sender = ((update.get("message") or {}).get("from") or {})
                if sender.get("id"):
                    user = sender
                    break
        except Exception as exc:
            print(f"  (poll issue: {exc})")
            time.sleep(2)

    if not user:
        print("[WARN] no message received. Add your user id to allowed_user_ids later.")
        print("       (Message the bot and it will tell you your id.)")
        users = []
    else:
        print(f"[ok] got message from {user.get('first_name')} (id {user['id']})")
        users = [user["id"]]

    cfg = load_config(cfg_path)
    cfg["telegram_bot_token"] = ""
    cfg["allowed_user_ids"] = users
    if not cfg.get("default_cwd"):
        # The bridge normally sits beside the projects it works on, so aim at a
        # sibling "DSprojects" -- but only when one is really there. Installed
        # inside DSprojects (the usual case) the parent *is* the projects
        # folder, and appending the name would produce DSprojects\DSprojects,
        # a directory that does not exist. The session then fails to start.
        parent = os.path.dirname(root_dir.rstrip("\\/"))
        sibling = os.path.join(parent, "DSprojects")
        cfg["default_cwd"] = sibling if os.path.isdir(sibling) else parent
    if not cfg.get("allowed_roots"):
        cfg["allowed_roots"] = [cfg["default_cwd"]]
    save_json_atomic(cfg_path, cfg)
    print(f"[ok] wrote {cfg_path}")

    print("\nNext:")
    print(f"  python \"{os.path.join(root_dir, 'bridge.py')}\" --check")
    print(f"  run-bridge.bat          (or: python bridge.py)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram <-> Claude Code bridge")
    parser.add_argument("--setup", action="store_true", help="interactive first-time setup")
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--cwd", default=None, help="override working directory")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    root_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = args.config or os.path.join(root_dir, "config.json")

    if args.setup:
        setup_logging(None, False)
        return cmd_setup(cfg_path, root_dir)

    load_env_file(os.path.join(root_dir, ".env"))
    setup_logging(os.path.join(root_dir, "bridge.log"), args.verbose)
    cfg = load_config(cfg_path)

    if args.cwd:
        cfg["default_cwd"] = os.path.abspath(args.cwd)

    if args.check:
        return cmd_check(cfg, root_dir)

    if not cfg.get("telegram_bot_token"):
        log.error("No bot token. Put TELEGRAM_BOT_TOKEN in .env, or run: "
                  "python bridge.py --setup")
        return 2
    if not (cfg.get("allowed_user_ids") or cfg.get("allowed_chat_ids")):
        log.error("allowed_user_ids is empty — refusing to start an open relay to "
                  "your desktop. Run: python bridge.py --setup")
        return 2

    bridge = Bridge(cfg, root_dir)
    try:
        bridge.run()
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        bridge.running = False
        for worker in bridge.workers.values():
            worker.queue.put(None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

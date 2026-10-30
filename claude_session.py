"""A long-lived `claude` process spoken to over the stream-json protocol.

Rather than spawning `claude -p` once per message, the bridge keeps one child
process alive and writes user turns onto its stdin as newline-delimited JSON.
That buys token-streamed progress, genuine conversation memory, and no
per-message startup cost -- which matters a lot here, because a fresh headless
launch measured ~91k uncached input tokens on the first turn.

Wire format (one JSON object per line):
  in : {"type":"user","message":{"role":"user","content":[{"type":"text",...}]}}
  out: system/init, assistant, user (tool results), stream_event, result
A `result` event terminates a turn.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time

IS_WINDOWS = os.name == "nt"

# On Windows a .cmd/.bat shim cannot be handed to CreateProcess directly; it
# has to go through the command interpreter.
_BATCH_SUFFIXES = (".cmd", ".bat")


def find_claude_command(explicit: str = "") -> str:
    """Locate the claude CLI.

    `shutil.which("claude")` returns None on this machine even though the CLI is
    on PATH -- PowerShell resolves the npm shim and Python does not -- so the
    known install locations are probed explicitly.
    """
    if explicit:
        return explicit

    candidates = [
        shutil.which("claude"),
        shutil.which("claude.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\claude.CMD"),
        os.path.expanduser("~/.local/bin/claude.exe"),
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.claude/local/claude.exe"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    raise FileNotFoundError(
        "Could not find the 'claude' CLI. Set \"claude_command\" in config.json "
        "to its full path (e.g. C:\\Users\\you\\AppData\\Roaming\\npm\\claude.cmd)."
    )


def _wrap_for_windows(argv: list[str]) -> list[str]:
    if IS_WINDOWS and argv[0].lower().endswith(_BATCH_SUFFIXES):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    return argv


def _kill_tree(pid: int) -> bool:
    """Kill a process and every process below it. True if the tree was signalled.

    `Popen.terminate()`/`kill()` map to TerminateProcess and only ever touch the
    one process they name. Because the CLI is reached through an npm shim, the
    session we actually care about is a *grandchild*
    (cmd.exe -> claude.exe): terminating the shim leaves it alive and orphaned.
    Its stdin pipe dies with the wrapper, so it can never be spoken to again --
    but it keeps its memory. That leak accumulated 21 processes, ~2.4 GB,
    between 10/07 and 10/13.

    Windows: taskkill /T walks the child tree.
    POSIX:   the child is started with start_new_session=True, so its whole
             process group can be signalled at once.
    """
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0
        except Exception:
            return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


class ClaudeSession:
    """Owns one child `claude` process and its reader threads."""

    def __init__(
        self,
        *,
        command: str,
        cwd: str,
        model: str | None = None,
        permission_mode: str = "bypassPermissions",
        skip_permissions: bool = True,
        max_budget_usd: float | None = None,
        append_system_prompt: str | None = None,
        extra_dirs: tuple[str, ...] = (),
        include_partial: bool = False,
        resume_session_id: str | None = None,
        setting_sources: str | None = None,
        on_event=None,        # (kind: str, payload: dict) -> None
        on_turn_end=None,     # (result_event: dict) -> None
        on_exit=None,         # (returncode: int, expected: bool) -> None
        on_log=None,          # (message: str) -> None
        env_overrides: dict | None = None,
    ):
        self.command = command
        self.cwd = cwd
        self.model = model
        self.permission_mode = permission_mode
        self.skip_permissions = skip_permissions
        self.max_budget_usd = max_budget_usd
        self.append_system_prompt = append_system_prompt
        self.extra_dirs = tuple(extra_dirs)
        self.include_partial = include_partial
        self.setting_sources = setting_sources
        self.on_event = on_event
        self.on_turn_end = on_turn_end
        self.on_exit = on_exit
        self.on_log = on_log
        self.env_overrides = env_overrides or {}

        self.session_id: str | None = resume_session_id
        self.busy = False
        self.started_at: float = 0.0
        # Cumulative across the session, as reported by the CLI (see _dispatch).
        self.total_cost_usd = 0.0
        self.turn_count = 0
        self.last_error: str | None = None

        self._proc: subprocess.Popen | None = None
        self._expected_exit = False
        self._resume_on_start: str | None = None
        self._generation = 0
        self._lock = threading.Lock()
        self._stderr_tail: list[str] = []

    # -- argv -------------------------------------------------------------

    def build_argv(self) -> list[str]:
        argv = [
            self.command,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self.skip_permissions:
            argv.append("--dangerously-skip-permissions")
        else:
            # Never let a headless run block on a prompt nobody can answer.
            argv += ["--permission-mode", self.permission_mode,
                     "--permission-prompts", "none"]
        if self.model:
            argv += ["--model", self.model]
        if self.max_budget_usd:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        if self.include_partial:
            argv.append("--include-partial-messages")
        if self.append_system_prompt:
            argv += ["--append-system-prompt", self.append_system_prompt]
        if self.setting_sources:
            argv += ["--setting-sources", self.setting_sources]
        if self._resume_on_start:
            argv += ["--resume", self._resume_on_start]
        for directory in self.extra_dirs:
            argv += ["--add-dir", directory]
        return argv

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self.alive:
            return
        if self.session_id:
            self._resume_on_start = self.session_id

        argv = _wrap_for_windows(self.build_argv())
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.update(self.env_overrides)

        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            # Own process group, so stop() can signal the whole tree (_kill_tree).
            kwargs["start_new_session"] = True

        self._log(f"starting: {' '.join(argv)}  (cwd={self.cwd})")
        self._expected_exit = False
        self._stderr_tail = []
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise RuntimeError(f"failed to launch claude: {exc}") from exc

        self._generation += 1
        generation = self._generation
        self.started_at = time.time()
        self.last_error = None
        self._resume_on_start = None

        threading.Thread(target=self._read_stdout, args=(generation,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(generation,), daemon=True).start()
        threading.Thread(target=self._watch_exit, args=(generation,), daemon=True).start()

    def stop(self, timeout: float = 5.0) -> None:
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        self._expected_exit = True
        # Take the whole tree, not just the shim we hold a handle to -- see
        # _kill_tree for why terminating the shim alone orphans the session.
        if not _kill_tree(proc.pid):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def interrupt(self) -> None:
        """Abort the current turn.

        The stream-json protocol has no cancel channel in this direction, so the
        child is killed; the caller restarts it with --resume, which preserves
        the conversation but drops the in-flight work.
        """
        self._log("interrupting turn (killing child process)")
        self.busy = False
        self.stop()

    @property
    def alive(self) -> bool:
        return bool(self._proc) and self._proc.poll() is None

    # -- outbound ---------------------------------------------------------

    def send(self, text: str) -> None:
        if not self.alive:
            raise RuntimeError("claude process is not running")
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._lock:
            proc = self._proc
            if not proc or not proc.stdin:
                raise RuntimeError("claude process is not running")
            proc.stdin.write(line)
            proc.stdin.flush()
        self.busy = True

    # -- inbound ----------------------------------------------------------

    def _read_stdout(self, generation: int) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self._log(f"non-JSON stdout: {line[:300]}")
                    continue
                if event.get("session_id"):
                    self.session_id = event["session_id"]
                try:
                    self._dispatch(event)
                except Exception as exc:  # never let a handler kill the reader
                    self._log(f"event handler error: {exc!r}")
        finally:
            if generation == self._generation:
                self._log("stdout closed")

    def _dispatch(self, event: dict) -> None:
        etype = event.get("type")

        if etype == "result":
            self.busy = False
            self.turn_count += 1
            # Verified against a live session: stream-json reports this
            # cumulatively for the whole session, so it is assigned rather than
            # summed. Callers wanting a single turn's cost must take a delta.
            reported = float(event.get("total_cost_usd") or 0.0)
            if reported:
                self.total_cost_usd = reported
            if event.get("is_error"):
                self.last_error = str(event.get("result") or event.get("subtype"))
            self._emit("result", event)
            if self.on_turn_end:
                self.on_turn_end(event)
            return

        if etype == "assistant":
            self._emit("assistant", event)
        elif etype == "user":
            self._emit("tool_result", event)
        elif etype == "system":
            if event.get("subtype") == "init":
                self._log(f"session ready: {self.session_id}")
            self._emit("system", event)
        elif etype == "stream_event":
            self._emit("partial", event)
        else:
            self._emit("other", event)

    def _emit(self, kind: str, payload: dict) -> None:
        if self.on_event:
            self.on_event(kind, payload)

    def _read_stderr(self, generation: int) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        for raw in proc.stderr:
            line = raw.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            del self._stderr_tail[:-40]
            self._log(f"stderr: {line}")

    def _watch_exit(self, generation: int) -> None:
        proc = self._proc
        if not proc:
            return
        code = proc.wait()
        if generation != self._generation:
            return  # a newer process has already replaced this one
        self.busy = False
        expected = self._expected_exit
        tail = "\n".join(self._stderr_tail[-8:])
        self._log(f"claude exited rc={code}{' (expected)' if expected else ''}")
        if not expected and tail:
            self._log(f"last stderr:\n{tail}")
        if self.on_exit:
            self.on_exit(code, expected)

    def _log(self, message: str) -> None:
        if self.on_log:
            self.on_log(message)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def result_text(event: dict) -> str:
        """The assistant's final prose from a `result` event."""
        text = event.get("result")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return ""

    @staticmethod
    def extract_blocks(message: dict) -> tuple[list[str], list[dict]]:
        """Split an assistant message into (text blocks, tool_use blocks)."""
        texts: list[str] = []
        tools: list[dict] = []
        content = (message or {}).get("content")
        if isinstance(content, str):
            return ([content] if content.strip() else []), []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                tools.append(block)
        return texts, tools


def describe_tool(tool: dict, width: int = 60) -> str:
    """One-line, phone-sized summary of a tool call."""
    name = tool.get("name", "tool")
    tool_input = tool.get("input") or {}

    detail = ""
    for key in ("command", "file_path", "path", "pattern", "url", "query",
                "prompt", "description", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            break
    if not detail and tool_input:
        try:
            detail = json.dumps(tool_input, ensure_ascii=False)
        except Exception:
            detail = ""
    detail = detail.replace("\n", " ")
    if len(detail) > width:
        detail = detail[: width - 1] + "…"
    return f"{name}: {detail}" if detail else name

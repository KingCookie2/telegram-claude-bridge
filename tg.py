"""Minimal, dependency-free Telegram Bot API client.

Only the endpoints this bridge needs. Two details that are easy to get wrong,
handled here rather than at every call site:

* Telegram measures message length in UTF-16 code units, so an emoji costs 2
  and a naive ``len()`` split gets rejected with "message is too long".
* Telegram's HTML parse mode rejects the *whole* message if any entity is
  malformed, so every markdown send falls back to plain text on failure.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

MAX_MESSAGE_LEN = 4096  # Telegram's cap, in UTF-16 code units
API_ROOT = "https://api.telegram.org/bot{token}/{method}"
FILE_ROOT = "https://api.telegram.org/file/bot{token}/{path}"


class TelegramError(RuntimeError):
    def __init__(self, method, code, description, retry_after=None):
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def u16_len(text: str) -> int:
    """Length in UTF-16 code units -- what Telegram actually counts."""
    return len(text.encode("utf-16-le")) // 2


def _take_prefix(text: str, limit: int) -> str:
    """Longest prefix of `text` that fits in `limit` UTF-16 units."""
    if u16_len(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if u16_len(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split into chunks that each fit Telegram's limit.

    Breaks on line boundaries where possible, and closes/reopens ``` fences
    across a break so every chunk renders as valid markdown on its own.
    """
    if not text:
        return []
    if u16_len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    reopen = False

    while remaining:
        prefix = "```\n" if reopen else ""
        # Room for the closing fence. It is only appended when the chunk ends
        # inside a block, but that is not known until the cut is made, and a
        # chunk that overshoots the cap is rejected outright by Telegram.
        budget = max(limit - u16_len(prefix) - 4, 1)

        if u16_len(remaining) <= budget:
            head, remaining = remaining, ""
        else:
            head = _take_prefix(remaining, budget)
            cut = head.rfind("\n")
            if cut >= len(head) // 2:  # only break on a newline worth taking
                head = head[: cut + 1]
            remaining = remaining[len(head):]

        body = prefix + head
        if body.count("```") % 2 == 1:
            body += "\n```"
            reopen = bool(remaining)
        else:
            reopen = False
        chunks.append(body)

    return chunks


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)


def md_to_html(text: str) -> str:
    """Conservative markdown -> Telegram HTML.

    Deliberately leaves underscores alone: `snake_case` identifiers are far
    more common in this bot's traffic than `_emphasis_` is, and a false
    positive silently mangles code.
    """
    stash: list[str] = []

    def _stash(m):
        lang = m.group(1).strip()
        cls = f' class="language-{_esc(lang)}"' if lang else ""
        stash.append(f"<pre><code{cls}>{_esc(m.group(2).rstrip())}</code></pre>")
        return f"\x00{len(stash) - 1}\x00"

    body = _FENCE_RE.sub(_stash, text)
    # An unterminated fence would swallow everything after it; keep it literal.
    body = body.replace("```", "\x01")
    body = _esc(body)

    body = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", body)
    body = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", body)
    body = re.sub(r"(?<![\w*])\*([^\n*]+?)\*(?![\w*])", r"<i>\1</i>", body)
    body = re.sub(r"^#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", body, flags=re.M)
    body = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', body)
    body = body.replace("\x01", "```")

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], body)


def _multipart(fields: dict, files: dict) -> tuple[str, bytes]:
    boundary = "----claudeBridge" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for key, (filename, payload) in files.items():
        out += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        out += payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return boundary, bytes(out)


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class Telegram:
    def __init__(self, token: str, timeout: float = 65.0):
        self.token = token
        self.timeout = timeout
        self.username: str | None = None
        self._opener = urllib.request.build_opener()

    # -- plumbing ---------------------------------------------------------

    def call(self, method, params=None, files=None, timeout=None, _tries=4):
        url = API_ROOT.format(token=self.token, method=method)
        headers = {}
        if files:
            boundary, body = _multipart(params or {}, files)
            data = body
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif params is not None:
            data = json.dumps(params).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None

        last: Exception | None = None
        for attempt in range(_tries):
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if not payload.get("ok"):
                    raise TelegramError(
                        method, payload.get("error_code"), payload.get("description", "")
                    )
                return payload.get("result")

            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                code = payload.get("error_code", exc.code)
                desc = payload.get("description", raw[:300])
                retry_after = (payload.get("parameters") or {}).get("retry_after")
                err = TelegramError(method, code, desc, retry_after)

                if code == 429 and attempt < _tries - 1:
                    time.sleep(min(retry_after or 2 ** attempt, 30))
                    last = err
                    continue
                if code >= 500 and attempt < _tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    last = err
                    continue
                raise err

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = TelegramError(method, 0, f"network: {exc}")
                if attempt < _tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last

        raise last or TelegramError(method, 0, "exhausted retries")

    # -- methods ----------------------------------------------------------

    def get_me(self):
        me = self.call("getMe")
        self.username = me.get("username")
        return me

    def get_updates(self, offset=None, timeout=50, allowed_updates=None):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        return self.call("getUpdates", params, timeout=timeout + 20)

    def send_message(self, chat_id, text, parse_mode=None, reply_to=None,
                     silent=False, link_preview=False):
        params = {"chat_id": chat_id, "text": text, "disable_notification": silent}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_to:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        if not link_preview:
            params["link_preview_options"] = {"is_disabled": True}
        return self.call("sendMessage", params)

    def send_markdown(self, chat_id, text, reply_to=None, silent=False) -> list[int]:
        """Send markdown, split to fit, degrading to plain text if needed.

        Returns the ids of the messages sent.
        """
        ids: list[int] = []
        for chunk in split_message(text):
            try:
                msg = self.send_message(
                    chat_id, md_to_html(chunk), parse_mode="HTML",
                    reply_to=reply_to if not ids else None, silent=silent,
                )
            except TelegramError as exc:
                if exc.code != 400:
                    raise
                msg = self.send_message(
                    chat_id, chunk, reply_to=reply_to if not ids else None, silent=silent
                )
            ids.append(msg["message_id"])
        return ids

    def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self.call("editMessageText", params)

    def edit_markdown(self, chat_id, message_id, text):
        try:
            return self.edit_message_text(chat_id, message_id, md_to_html(text), "HTML")
        except TelegramError as exc:
            if exc.code != 400:
                raise
            return self.edit_message_text(chat_id, message_id, text)

    def send_chat_action(self, chat_id, action="typing"):
        try:
            return self.call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError:
            return None  # cosmetic only, never fatal

    def delete_message(self, chat_id, message_id):
        try:
            return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            return None

    def send_document(self, chat_id, path, caption=None):
        with open(path, "rb") as fh:
            payload = fh.read()
        fields = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1000]
        return self.call(
            "sendDocument", fields, files={"document": (os.path.basename(path), payload)}
        )

    def download_file(self, file_id, dest_dir) -> str:
        info = self.call("getFile", {"file_id": file_id})
        remote = info["file_path"]
        os.makedirs(dest_dir, exist_ok=True)
        name = os.path.basename(remote) or f"file_{uuid.uuid4().hex[:8]}"
        dest = os.path.join(dest_dir, name)
        url = FILE_ROOT.format(token=self.token, path=remote)
        with self._opener.open(url, timeout=120) as resp, open(dest, "wb") as out:
            while True:
                block = resp.read(65536)
                if not block:
                    break
                out.write(block)
        return dest

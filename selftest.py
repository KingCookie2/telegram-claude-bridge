"""Offline tests for the pieces that are easy to get subtly wrong.

    python selftest.py

Covers Telegram's UTF-16 length accounting, the message splitter (including
fence balancing), the markdown->HTML conversion, and the argv the bridge hands
to the claude CLI. No network, no Telegram account, no Claude session.
"""

from __future__ import annotations

import sys

import tg
from claude_session import ClaudeSession, describe_tool, find_claude_command

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f"\n       {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------
section("utf-16 length accounting")

check("ascii", tg.u16_len("abc") == 3)
check("emoji counts as 2", tg.u16_len("😀") == 2, f"got {tg.u16_len('😀')}")
check("mixed", tg.u16_len("a😀b") == 4, f"got {tg.u16_len('a😀b')}")
check("naive len would be wrong", tg.u16_len("😀") != len("😀"))


# --------------------------------------------------------------------------
section("message splitting")

short = tg.split_message("hello")
check("short text is one chunk", short == ["hello"], repr(short))
check("empty text yields nothing", tg.split_message("") == [])

# A body of plain lines, comfortably over the limit.
plain = "\n".join(f"line {i} " + "x" * 60 for i in range(300))
chunks = tg.split_message(plain)
check("plain text splits into several chunks", len(chunks) > 1, f"{len(chunks)} chunks")
check("no chunk exceeds the cap",
      all(tg.u16_len(c) <= tg.MAX_MESSAGE_LEN for c in chunks),
      f"widest {max(tg.u16_len(c) for c in chunks)}")
check("nothing is lost", "".join(chunks).replace("\n", "") == plain.replace("\n", ""))

# Emoji-heavy text: naive len() would under-count and overshoot.
heavy = "😀" * 4000
hchunks = tg.split_message(heavy)
check("emoji text splits", len(hchunks) > 1, f"{len(hchunks)} chunks")
check("emoji chunks respect the utf-16 cap",
      all(tg.u16_len(c) <= tg.MAX_MESSAGE_LEN for c in hchunks),
      f"widest {max(tg.u16_len(c) for c in hchunks)}")
check("emoji text survives intact", "".join(hchunks) == heavy)

# A fenced block long enough to cross a boundary must stay balanced per chunk.
fenced = "before\n```python\n" + "\n".join(f"print({i})" for i in range(500)) + "\n```\nafter"
fchunks = tg.split_message(fenced)
check("fenced text splits", len(fchunks) > 1, f"{len(fchunks)} chunks")
check("every chunk has balanced fences",
      all(c.count("```") % 2 == 0 for c in fchunks),
      "; ".join(f"{c.count('```')} fences" for c in fchunks))
check("fenced chunks respect the cap",
      all(tg.u16_len(c) <= tg.MAX_MESSAGE_LEN for c in fchunks),
      f"widest {max(tg.u16_len(c) for c in fchunks)}")


# --------------------------------------------------------------------------
section("markdown -> telegram html")

check("escapes angle brackets", tg.md_to_html("a < b") == "a &lt; b")
check("escapes ampersands once", "&amp;amp;" not in tg.md_to_html("a & b"))
check("bold", tg.md_to_html("**hi**") == "<b>hi</b>")
check("inline code", tg.md_to_html("`x = 1`") == "<code>x = 1</code>")
check("heading becomes bold", tg.md_to_html("## Title") == "<b>Title</b>")
check("link", tg.md_to_html("[a](https://x.dev)") == '<a href="https://x.dev">a</a>')
check("underscores untouched", tg.md_to_html("snake_case_name") == "snake_case_name")
check("code inside a fence is escaped not styled",
      tg.md_to_html("```\nif a < b: pass\n```")
      == "<pre><code>if a &lt; b: pass</code></pre>")
check("fenced language tag", 'class="language-py"' in tg.md_to_html("```py\nx\n```"))

# The stash/restore dance must not leak placeholders.
mixed = "# Head\n\ntext with `code` and **bold**\n\n```js\nconst a = 1;\n```\n\nend"
out = tg.md_to_html(mixed)
check("no placeholder leaks", "\x00" not in out and "\x01" not in out, repr(out[:120]))
check("all parts present",
      "<b>Head</b>" in out and "<code>code</code>" in out
      and "<b>bold</b>" in out and "<pre><code" in out and "end" in out)

# An unterminated fence must not swallow the rest of the message.
unterminated = "before\n```\nstuck\nand more text"
uout = tg.md_to_html(unterminated)
check("unterminated fence stays literal", "and more text" in uout, repr(uout))


# --------------------------------------------------------------------------
section("claude command discovery")

try:
    found = find_claude_command()
    check("claude cli found", bool(found), found)
    print(f"       -> {found}")
except FileNotFoundError as exc:
    check("claude cli found", False, str(exc))

check("explicit path wins", find_claude_command(r"C:\custom\claude.cmd")
      == r"C:\custom\claude.cmd")


# --------------------------------------------------------------------------
section("claude argv")

session = ClaudeSession(
    command="claude",
    cwd=".",
    model="sonnet",
    skip_permissions=True,
    append_system_prompt="be brief",
    extra_dirs=(r"C:\one", r"C:\two"),
    include_partial=False,
)
argv = session.build_argv()
check("stream-json in", "--input-format" in argv and "stream-json" in argv)
check("stream-json out", "--output-format" in argv)
check("verbose", "--verbose" in argv)
check("skip permissions", "--dangerously-skip-permissions" in argv)
check("model passed", "sonnet" in argv)
check("system prompt passed", "be brief" in argv)
check("extra dirs passed", argv.count("--add-dir") == 2)
check("no prompt permissions flag when bypassing",
      "--permission-prompts" not in argv)

guarded = ClaudeSession(command="claude", cwd=".", skip_permissions=False)
gargv = guarded.build_argv()
check("guarded mode sets permission-mode", "--permission-mode" in gargv)
check("guarded mode disables prompts", "none" in gargv)

resumed = ClaudeSession(command="claude", cwd=".", resume_session_id="abc-123")
resumed._resume_on_start = "abc-123"
check("resume flag", "--resume" in resumed.build_argv())


# --------------------------------------------------------------------------
section("session helpers")

texts, tools = ClaudeSession.extract_blocks({"content": [
    {"type": "text", "text": "hello"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
    {"type": "text", "text": "   "},
]})
check("text blocks extracted", texts == ["hello"], repr(texts))
check("tool blocks extracted", len(tools) == 1)

check("string content handled",
      ClaudeSession.extract_blocks({"content": "hi"})[0] == ["hi"])
check("result text", ClaudeSession.result_text({"result": " done "}) == "done")
check("empty result text", ClaudeSession.result_text({"result": "  "}) == "")

line = describe_tool({"name": "Read", "input": {"file_path": r"C:\a\b\c.py"}})
check("tool summary names the tool", line.startswith("Read:"), line)
check("long tool detail is truncated",
      len(describe_tool({"name": "Bash", "input": {"command": "x" * 500}})) < 100)


# --------------------------------------------------------------------------
print()
if FAILED:
    print(f"{len(FAILED)} failed, {PASSED} passed")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)

print(f"all {PASSED} checks passed")

"""A local JSONL log of the ways an LLM struggles, for later prompt work.

These events are the recoverable failures a small model hits: a malformed
edit, a trace it asked for badly, a name that was too common, a context
window it blew. Nothing here is sent anywhere; it is a file you can read
after a session to see what to fix next.
"""

import json
import re
from datetime import datetime
from pathlib import Path

# Qwen and friends emit their own tool-calling XML instead of the fence
TOOL_XML_RE = re.compile(
    r"</?tool_call>|</?function\b|</?parameter\b|<tool_call>",
    re.IGNORECASE,
)

# A fenced ```trace that we failed to honour. Loose prose is handled separately.
TRACE_ATTEMPT_RE = re.compile(r"```+\s*trace\b", re.IGNORECASE)

FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

EXCERPT_LEN = 240
MAX_ENTRIES = 200


def outside_fences(content):
    """The parts of a reply that are not inside a fenced code block."""

    if not content:
        return ""

    kept = []
    in_fence = False
    fence_char = None
    for line in content.splitlines():
        fence = FENCE_LINE_RE.match(line)
        if fence and in_fence and line.strip()[0] == fence_char:
            in_fence = False
            fence_char = None
            continue
        if fence and not in_fence:
            in_fence = True
            fence_char = line.strip()[0]
            continue
        if not in_fence:
            kept.append(line)
    return "\n".join(kept)


def excerpt(text, limit=EXCERPT_LEN):
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class LimitationLog:
    """Append-only JSONL of LLM limitations. Never raises."""

    def __init__(self, path=None, io=None, enabled=True, model=None, edit_format=None):
        self.path = Path(path) if path else None
        self.io = io
        self.enabled = bool(enabled and self.path)
        self.model = model
        self.edit_format = edit_format
        self.entries = []

    def record(self, kind, **fields):
        if not self.enabled:
            return

        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
        }
        if self.model:
            entry["model"] = self.model
        if self.edit_format:
            entry["edit_format"] = self.edit_format

        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                entry[key] = value
            elif isinstance(value, (list, tuple, set)):
                entry[key] = [str(item) for item in value]
            else:
                entry[key] = str(value)

        self.entries.append(entry)
        if len(self.entries) > MAX_ENTRIES:
            self.entries = self.entries[-MAX_ENTRIES:]

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as err:
            if self.io:
                self.io.tool_warning(f"Unable to write LLM limitation log {self.path}: {err}")
            self.enabled = False

    def looks_like_tool_xml(self, content):
        return bool(TOOL_XML_RE.search(outside_fences(content)))

    def looks_like_trace_attempt(self, content):
        return bool(content and TRACE_ATTEMPT_RE.search(content))

import re
from pathlib import Path

from .parsing import parse_need, strip_reasoning

SNIPPET_RE = re.compile(r"^\s*(?:snippet|- snippet)\s*:\s*(?P<ref>.+?)\s*$", re.IGNORECASE)


def extract_snippet_refs(text):
    """The `snippet: file::symbol` lines an architect put in a brief."""
    refs = []
    for line in (text or "").splitlines():
        match = SNIPPET_RE.match(line)
        if match:
            refs.append((line, match.group("ref").strip()))
    return refs


def resolve_snippets(brief_text, knowledge, max_tokens, token_count=None):
    """Replace snippet requests with the actual read-only code.

    The worker never sees the repo map, so anything it needs from other files
    has to be inlined here - capped, because its context is small too.
    """
    refs = extract_snippet_refs(brief_text)
    if not refs:
        return brief_text, []

    token_count = token_count or (lambda text: max(1, len(text) // 4))
    used = 0
    resolved = []
    text = brief_text

    for line, ref in refs:
        need = parse_need(f"source {ref}")
        block = ""
        if need is not None:
            rel_fname = knowledge._match_file(need.target) or need.target
            body, label = knowledge._slice(rel_fname, need)
            if body:
                remaining = max(0, max_tokens - used)
                if remaining < 40:
                    block = f"(snippet omitted, no room left: {ref})"
                else:
                    body = knowledge.clamp(body, remaining)
                    lang = Path(rel_fname).suffix.lstrip(".")
                    block = f"# {label}\n```{lang}\n{body}\n```"
                    used += token_count(block)
                    resolved.append(label)
        if not block:
            block = f"(could not resolve snippet: {ref})"
        text = text.replace(line, block, 1)

    return text, resolved


def clean_brief(text, reasoning_tag=None):
    """Strip reasoning and protocol lines so the worker sees only the brief."""
    text = strip_reasoning(text, reasoning_tag)
    keep = []
    for line in text.splitlines():
        lowered = line.strip().lower()
        if lowered.startswith(("need:", "remember:", "remember pinned:", "forget ", "question:")):
            continue
        if lowered.startswith(("verdict:", "note:")):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def brief_path(root, task_id):
    return Path(root) / ".aider.pipeline" / "briefs" / f"{task_id}.md"


def save_brief(root, task_id, text):
    path = brief_path(root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_brief(root, task_id):
    path = brief_path(root, task_id)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


WORKER_PREAMBLE = """You are the WORKER in a two-model pipeline.
Do exactly what the brief says, and nothing else.
You may only change the one file named in the brief.
Do not refactor, reformat or improve anything the brief does not ask for.
If something in the brief is impossible, make the smallest change that satisfies the rest
and say so in one line after your edit.

"""


def worker_message(brief_text):
    return WORKER_PREAMBLE + brief_text.strip()

import re

import yaml

# Local models drift from any output format, so every parser here is lenient:
# it looks for the signal and ignores surrounding prose.

NEED_KINDS = ("outline", "refs", "grep", "digest", "source", "about", "trace")

# "remember pinned" has to come before "remember" or the alternation eats it.
DIRECTIVE_RE = re.compile(
    r"^\s*(need|remember\s+pinned|remember|forget|question|verdict|note)\s*:?\s*(.*)$",
    re.IGNORECASE,
)
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$")
SOURCE_RANGE_RE = re.compile(r"^(?P<file>.+?):L(?P<start>\d+)\s*-\s*L?(?P<end>\d+)$", re.I)
VERDICT_RE = re.compile(r"\b(ACCEPT|RETRY|REPLAN|FIX_CODE|FIX_TEST|ACCEPT_KNOWN|ESCALATE)\b")
COMPACT_RE = re.compile(r"^\s*(keep|drop|rewrite)\s+(\S+)\s*:?\s*(.*)$", re.IGNORECASE)


class Need:
    __slots__ = ("kind", "target", "symbol", "start_line", "end_line", "raw")

    def __init__(self, kind, target="", symbol="", start_line=None, end_line=None, raw=""):
        self.kind = kind
        self.target = target
        self.symbol = symbol
        self.start_line = start_line
        self.end_line = end_line
        self.raw = raw

    def key(self):
        return (self.kind, self.target, self.symbol, self.start_line, self.end_line)

    def __repr__(self):
        return f"Need({self.kind} {self.target} {self.symbol})"

    def __eq__(self, other):
        return isinstance(other, Need) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())


class Directives:
    def __init__(self):
        self.needs = []
        self.remember = []  # list of (text, pinned)
        self.forget = []
        self.questions = []
        self.notes = []
        self.verdict = None
        self.body = ""

    def __repr__(self):
        return (
            f"Directives(verdict={self.verdict}, needs={self.needs},"
            f" remember={len(self.remember)}, questions={len(self.questions)})"
        )


def strip_reasoning(text, tag=None):
    """Remove <think> style reasoning blocks so directives are not read from them."""
    if not text:
        return ""
    tags = {t for t in (tag, "think", "thinking", "reasoning") if t}
    for name in tags:
        text = re.sub(
            rf"<{name}>.*?</{name}>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # An unterminated opening tag means everything after it is reasoning.
        text = re.sub(rf"<{name}>.*\Z", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def iter_fenced_blocks(text):
    """Yield (info_string, body) for each fenced block in the text."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = FENCE_RE.match(lines[index])
        if not match:
            index += 1
            continue
        fence, info = match.group(1), match.group(2).lower()
        body = []
        index += 1
        while index < len(lines):
            closing = FENCE_RE.match(lines[index])
            if closing and closing.group(1)[0] == fence[0]:
                break
            body.append(lines[index])
            index += 1
        index += 1
        yield info, "\n".join(body)


def extract_yaml(text, key=None):
    """Parse the most likely YAML payload out of a model reply.

    Prefers a ```yaml fenced block, then the largest fenced block, then the
    whole reply. Returns the parsed object, or raises ValueError.
    """
    text = strip_reasoning(text)
    candidates = []
    blocks = list(iter_fenced_blocks(text))
    candidates += [body for info, body in blocks if info in ("yaml", "yml")]
    candidates += sorted(
        (body for info, body in blocks if info not in ("yaml", "yml")),
        key=len,
        reverse=True,
    )
    if not blocks:
        candidates.append(text)

    errors = []
    for candidate in candidates:
        if not candidate.strip():
            continue
        try:
            data = yaml.safe_load(candidate)
        except yaml.YAMLError as err:
            errors.append(str(err).splitlines()[0] if str(err) else "invalid YAML")
            continue
        if data is None:
            continue
        if key is not None and isinstance(data, dict) and key in data:
            return data[key]
        return data

    detail = f" ({errors[0]})" if errors else ""
    raise ValueError(f"Could not find a YAML block in the reply{detail}.")


def parse_need(text):
    """Parse the argument of a NEED: line into a Need, or None."""
    text = text.strip().strip("`")
    if not text:
        return None

    parts = text.split(None, 1)
    kind = parts[0].lower().rstrip(":")
    rest = parts[1].strip() if len(parts) > 1 else ""
    if kind not in NEED_KINDS:
        return None
    if not rest:
        return None

    rest = rest.strip("`").strip()

    if kind in ("refs", "about", "trace"):
        symbol = rest.split()[0].strip("(),")
        return Need(kind, symbol=symbol, raw=text)

    if kind == "grep":
        return Need(kind, target=rest, raw=text)

    if kind in ("digest", "source"):
        if "::" in rest:
            target, symbol = rest.split("::", 1)
            return Need(kind, target=_norm_path(target), symbol=symbol.strip(), raw=text)
        match = SOURCE_RANGE_RE.match(rest)
        if match:
            start = int(match.group("start"))
            end = int(match.group("end"))
            return Need(
                kind,
                target=_norm_path(match.group("file")),
                start_line=min(start, end),
                end_line=max(start, end),
                raw=text,
            )
        return Need(kind, target=_norm_path(rest.split()[0]), raw=text)

    # outline
    return Need(kind, target=_norm_path(rest.split()[0]), raw=text)


def _norm_path(path):
    return path.strip().strip("`'\"").replace("\\", "/").strip()


def parse_directives(text, reasoning_tag=None):
    """Pull protocol lines out of an architect reply.

    Lines inside fenced blocks are left alone so briefs and YAML survive.
    """
    result = Directives()
    text = strip_reasoning(text, reasoning_tag)

    body_lines = []
    in_fence = None
    for line in text.splitlines():
        fence = FENCE_RE.match(line)
        if fence:
            if in_fence and fence.group(1)[0] == in_fence[0]:
                in_fence = None
            elif not in_fence:
                in_fence = fence.group(1)
            body_lines.append(line)
            continue

        if in_fence:
            body_lines.append(line)
            continue

        match = DIRECTIVE_RE.match(line)
        if not match:
            body_lines.append(line)
            continue

        keyword = " ".join(match.group(1).lower().split())
        value = match.group(2).strip()

        if keyword == "need":
            need = parse_need(value)
            if need and need not in result.needs:
                result.needs.append(need)
            else:
                body_lines.append(line)
        elif keyword == "remember":
            if value:
                result.remember.append((value, False))
        elif keyword == "remember pinned":
            if value:
                result.remember.append((value, True))
        elif keyword == "forget":
            for token in re.split(r"[\s,]+", value):
                if token:
                    result.forget.append(token.strip())
        elif keyword == "question":
            if value:
                result.questions.append(value)
        elif keyword == "note":
            if value:
                result.notes.append(value)
        elif keyword == "verdict":
            found = VERDICT_RE.search(value.upper())
            if found:
                result.verdict = found.group(1)
            body_lines.append(line)

    result.body = "\n".join(body_lines).strip()
    return result


def find_verdict(text, allowed):
    """Best-effort verdict lookup for replies that forgot the VERDICT: prefix."""
    upper = strip_reasoning(text).upper()
    for line in upper.splitlines():
        if "VERDICT" in line:
            for word in allowed:
                if word in line:
                    return word
    for word in allowed:
        if re.search(rf"\b{word}\b", upper):
            return word
    return None


def parse_compact(text):
    """Parse COMPACT output into (keep_ids, drop_ids, rewrites)."""
    keep, drop, rewrites = [], [], {}
    for line in strip_reasoning(text).splitlines():
        match = COMPACT_RE.match(line)
        if not match:
            continue
        action = match.group(1).lower()
        item_id = match.group(2).strip().strip(":,")
        payload = match.group(3).strip()
        if action == "keep":
            keep.append(item_id)
        elif action == "drop":
            drop.append(item_id)
        elif action == "rewrite" and payload:
            rewrites[item_id] = payload
            keep.append(item_id)
    return keep, drop, rewrites

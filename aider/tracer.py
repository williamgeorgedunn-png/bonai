"""Deterministic call/usage tracing over the repo map's symbol index.

The repo map shows *definitions*. This module answers the follow-up questions a
human asks next: who calls this, what does it call, where is this variable
written and read, and where does its value come from or go to.

Everything here is deterministic: no LLM is involved, results only depend on the
tree-sitter tags aider already extracts plus a word-boundary scan for variables.
Results are rendered as short snippets so the model can decide which files it
actually wants, instead of being handed whole files.
"""

import os
import re
from collections import defaultdict, namedtuple
from difflib import get_close_matches

from grep_ast import TreeContext, filename_to_lang

from aider.repomap import Scope

# tree_sitter is throwing a FutureWarning
from grep_ast.tsl import get_parser  # noqa: E402

# A single located usage of a symbol
Hit = namedtuple("Hit", "rel_fname line kind scope_name note")

# What the user/model asked for
TraceRequest = namedtuple("TraceRequest", "symbol direction depth file strict")

# Definition kinds that behave like callables/types rather than data
CALLABLE_KINDS = {
    "function",
    "method",
    "class",
    "interface",
    "constructor",
    "macro",
    "module",
    "type",
    "struct",
    "enum",
    "trait",
    "implementation",
}

DATA_KINDS = {"variable", "constant", "field", "property", "parameter"}

DIRECTIONS = ("up", "down", "both")

DIRECTION_WORDS = {
    "up": "up",
    "upstream": "up",
    "caller": "up",
    "callers": "up",
    "calledby": "up",
    "uses": "up",
    "used": "up",
    "usage": "up",
    "usages": "up",
    "users": "up",
    "refs": "up",
    "references": "up",
    "down": "down",
    "downstream": "down",
    "callee": "down",
    "callees": "down",
    "calls": "down",
    "body": "down",
    "both": "both",
    "all": "both",
    "full": "both",
}

MAX_SYMBOLS_PER_REQUEST = 3

# Beyond this a name is too common to trace usefully
MAX_DEFS_BEFORE_AMBIGUOUS = 6
MAX_FILES_BEFORE_AMBIGUOUS = 14

MAX_LOIS_PER_FILE = 8
MAX_LOIS_PER_SCOPE = 2
MAX_CALLEES = 12
MAX_SCAN_BYTES = 1_000_000

TEST_PATH_RE = re.compile(r"(^|/)(tests?|spec)(/|$)|(^|/)(test_[^/]*|[^/]*_test|[^/]*\.test)\.")

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*(?P<info>[^\s`~]*)\s*(?P<rest>.*?)\s*$")
LOOSE_TRACE_RE = re.compile(r"^\s*(?:[-*+]\s*)?/?trace\b[:\s]+(?P<body>.+?)\s*$", re.IGNORECASE)
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
DEPTH_RE = re.compile(r"^depth[=:]?(\d+)?$", re.IGNORECASE)

# Characters models wrap symbols in: backticks, quotes, parens, markdown emphasis
SYMBOL_STRIP = "`'\"*_()[]{}<>,.;:!?"

SYMBOL_RE = re.compile(r"^[A-Za-z_][\w.:/\\-]*$")


def is_test_path(rel_fname):
    return bool(TEST_PATH_RE.search(rel_fname.replace("\\", "/")))


def clean_symbol(text):
    """Normalize the many ways a model writes a symbol name."""

    if not text:
        return ""

    text = text.strip()
    text = text.strip(SYMBOL_STRIP)

    # foo(), foo(a, b) -> foo
    paren = text.find("(")
    if paren > 0:
        text = text[:paren]

    text = text.strip(SYMBOL_STRIP)

    # self.foo / this.foo / cls.foo -> foo, but keep Class.method
    for prefix in ("self.", "this.", "cls."):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]

    text = text.lstrip("@#$&")
    text = text.strip(SYMBOL_STRIP)

    if not text or not SYMBOL_RE.match(text):
        return ""

    return text


def parse_trace_line(line, strict=True):
    """Parse one line of a trace request into a TraceRequest, or None."""

    line = BULLET_RE.sub("", line.strip())
    if not line:
        return None

    # Split on commas and whitespace, so "foo, bar up" works
    tokens = [tok for tok in re.split(r"[,\s]+", line) if tok]
    if not tokens:
        return None

    symbol = None
    direction = "both"
    depth = 1
    file = None

    expect_file = False
    expect_depth = False

    for token in tokens:
        low = token.lower().strip(SYMBOL_STRIP)

        if expect_file:
            file = token.strip(SYMBOL_STRIP + "`")
            expect_file = False
            continue

        if expect_depth:
            expect_depth = False
            if low.isdigit():
                depth = max(1, min(2, int(low)))
                continue

        depth_match = DEPTH_RE.match(low)
        if depth_match:
            if depth_match.group(1):
                depth = max(1, min(2, int(depth_match.group(1))))
            else:
                expect_depth = True
            continue

        if low in DIRECTION_WORDS:
            direction = DIRECTION_WORDS[low]
            continue

        if low == "in":
            expect_file = True
            continue

        if symbol is None:
            symbol = clean_symbol(token)
            continue

        # Extra words are ignored: models like to add prose

    if not symbol:
        return None

    # file.py:symbol
    if file is None and ":" in symbol:
        head, _, tail = symbol.rpartition(":")
        if head and tail and ("/" in head or "." in head):
            file = head
            symbol = tail

    if not symbol:
        return None

    return TraceRequest(symbol=symbol, direction=direction, depth=depth, file=file, strict=strict)


def parse_trace_requests(content, max_requests=MAX_SYMBOLS_PER_REQUEST):
    """Find trace requests in an LLM reply.

    Accepts the documented ```trace fenced block, and also tolerates loose
    "trace foo" lines, which small models emit instead. Loose requests are
    marked non-strict so the caller can require that they resolve to a real
    symbol before acting on them.
    """

    if not content:
        return []

    requests = []
    seen = set()

    def add(req):
        if not req:
            return
        key = (req.symbol.lower(), req.direction, req.file)
        if key in seen:
            return
        seen.add(key)
        requests.append(req)

    in_trace_block = False
    in_other_block = False
    fence_char = None

    for line in content.splitlines():
        fence = FENCE_RE.match(line)
        if fence and (in_trace_block or in_other_block):
            # Only a matching fence character closes the block
            if line.strip()[0] == fence_char:
                in_trace_block = False
                in_other_block = False
                fence_char = None
                continue

        if fence and not (in_trace_block or in_other_block):
            info = fence.group("info").lower()
            fence_char = line.strip()[0]
            if info == "trace":
                in_trace_block = True
                # ```trace up  -> direction applies to the whole block
                rest = fence.group("rest")
                if rest:
                    add(parse_trace_line(rest))
            else:
                in_other_block = True
            continue

        if in_other_block:
            continue

        if in_trace_block:
            add(parse_trace_line(line))
            continue

        loose = LOOSE_TRACE_RE.match(line)
        if loose:
            add(parse_trace_line(loose.group("body"), strict=False))

    return requests[:max_requests]


class RepoTracer:
    """Answers "where is this used" questions against a SymbolIndex."""

    def __init__(self, repo_map, io, root, max_tokens=1024, verbose=False):
        self.repo_map = repo_map
        self.io = io
        self.root = root
        self.max_tokens = max_tokens
        self.verbose = verbose

        self.parse_cache = dict()
        self.text_cache = dict()
        self.snippet_context_cache = dict()

        # Set per trace: don't bury real call sites under test files, unless
        # the symbol being traced lives in the tests itself.
        self.prefer_tests = False

    ##
    # Plumbing
    ##

    def get_index(self, all_fnames, progress=None):
        return self.repo_map.get_symbol_index(all_fnames, progress=progress)

    def token_count(self, text):
        try:
            return self.repo_map.token_count(text)
        except Exception:
            return len(text) / 4

    def file_rank(self, rel_fname):
        return self.repo_map.last_ranked.get(rel_fname, 0)

    def file_sort_key(self, rel_fname):
        """Rank files by repo-map PageRank, keeping tests last by default."""

        test_penalty = 0 if self.prefer_tests else int(is_test_path(rel_fname))
        return (test_penalty, -self.file_rank(rel_fname), rel_fname)

    def read_text(self, abs_fname):
        try:
            mtime = os.path.getmtime(abs_fname)
            size = os.path.getsize(abs_fname)
        except OSError:
            return None

        if size > MAX_SCAN_BYTES:
            return None

        key = (abs_fname, mtime)
        if key in self.text_cache:
            return self.text_cache[key]

        text = self.io.read_text(abs_fname, silent=True)

        if len(self.text_cache) > 200:
            self.text_cache.clear()
        self.text_cache[key] = text

        return text

    def get_tree(self, abs_fname):
        """(lang, tree) for a file, cached by mtime. None when unparseable."""

        try:
            mtime = os.path.getmtime(abs_fname)
        except OSError:
            return None, None

        key = (abs_fname, mtime)
        if key in self.parse_cache:
            return self.parse_cache[key]

        result = (None, None)
        lang = filename_to_lang(abs_fname)
        if lang:
            code = self.read_text(abs_fname)
            if code:
                try:
                    parser = get_parser(lang)
                    result = (lang, parser.parse(bytes(code, "utf-8")))
                except Exception as err:
                    if self.verbose:
                        self.io.tool_warning(f"Unable to parse {abs_fname}: {err}")

        if len(self.parse_cache) > 25:
            self.parse_cache.clear()
        self.parse_cache[key] = result

        return result

    ##
    # Resolution
    ##

    def resolve(self, index, req):
        """Find the definitions a request refers to.

        Returns (name, container, defs). `defs` may be empty for variables,
        which most tags queries don't capture.
        """

        symbol = req.symbol
        container = None
        name = symbol

        if "." in symbol:
            container, _, name = symbol.rpartition(".")

        defs = list(index.defs.get(name, []))

        if container:
            filtered = []
            for tag in defs:
                scope = index.scope_for_def(tag)
                if not scope:
                    continue
                qualified = index.qualified_name(scope)
                if qualified == f"{container}.{name}" or qualified.endswith(f".{container}.{name}"):
                    filtered.append(tag)
            if filtered:
                defs = filtered

        if req.file:
            wanted = req.file.replace("\\", "/")
            in_file = [tag for tag in defs if tag.rel_fname.replace("\\", "/").endswith(wanted)]
            if in_file:
                defs = in_file

        return name, container, defs

    def symbol_kind(self, index, defs):
        kinds = set()
        for tag in defs:
            scope = index.scope_for_def(tag)
            if scope:
                kinds.add(scope.kind)

        if kinds & CALLABLE_KINDS:
            return "callable"
        if kinds & DATA_KINDS:
            return "data"
        if defs:
            return "callable"
        return "unknown"

    ##
    # Tracing
    ##

    def trace(self, req, all_fnames, chat_rel_fnames=(), max_tokens=None, progress=None):
        """Trace one symbol. Always returns text the model/user can act on."""

        max_tokens = max_tokens or self.max_tokens
        chat_rel_fnames = set(chat_rel_fnames or ())

        index = self.get_index(all_fnames, progress=progress)
        name, container, defs = self.resolve(index, req)

        # Only let test files lead the results when the symbol lives in tests
        self.prefer_tests = bool(defs) and all(is_test_path(tag.rel_fname) for tag in defs)

        if len(defs) > MAX_DEFS_BEFORE_AMBIGUOUS:
            return self.render_ambiguous_defs(index, req, name, defs)

        kind = self.symbol_kind(index, defs)

        if kind == "callable":
            return self.trace_callable(index, req, name, defs, chat_rel_fnames, max_tokens)

        return self.trace_data(index, req, name, defs, chat_rel_fnames, max_tokens)

    ##
    # Callables
    ##

    def trace_callable(self, index, req, name, defs, chat_rel_fnames, max_tokens):
        def_scopes = []
        for tag in defs:
            scope = index.scope_for_def(tag)
            if scope:
                def_scopes.append(scope)
            else:
                def_scopes.append(
                    Scope(tag.rel_fname, tag.fname, tag.name, "unknown", tag.line, tag.line)
                )

        sections = []
        header = self.describe_symbol(index, req.symbol, "function/class", def_scopes)
        sections.append(header)

        budget = max_tokens

        # Where it is defined
        if def_scopes:
            text = self.render_definitions(index, def_scopes)
            budget -= self.token_count(text)
            sections.append(text)

        callers_files = set()
        callee_files = set()

        if req.direction in ("up", "both"):
            hits, total, skipped_in_chat = self.find_callers(
                index, name, def_scopes, chat_rel_fnames
            )
            text, shown_files = self.render_hits(
                index,
                hits,
                total,
                max(budget * (0.7 if req.direction == "both" else 1.0), 200),
                title=f"Callers of `{name}`",
                empty=f"No calls to `{name}` found outside its own definition.",
                skipped_in_chat=skipped_in_chat,
            )
            callers_files = shown_files
            budget -= self.token_count(text)
            sections.append(text)

        if req.direction in ("down", "both"):
            text, callee_files = self.render_callees(index, name, def_scopes, chat_rel_fnames)
            budget -= self.token_count(text)
            sections.append(text)

        footer = self.render_footer(callers_files | callee_files, chat_rel_fnames)
        if footer:
            sections.append(footer)

        if not defs:
            sections.append(self.suggest_similar(index, name))

        return "\n".join(section for section in sections if section)

    def find_callers(self, index, name, def_scopes, chat_rel_fnames):
        """Reference sites for `name`, excluding its own body (recursion)."""

        hits = []
        total = 0
        skipped_in_chat = 0

        own_ranges = defaultdict(list)
        for scope in def_scopes:
            own_ranges[scope.rel_fname].append((scope.start_line, scope.end_line))

        for tag in index.refs.get(name, []):
            inside_own_def = any(
                start <= tag.line <= end for start, end in own_ranges.get(tag.rel_fname, [])
            )
            if inside_own_def:
                continue

            total += 1

            if tag.rel_fname in chat_rel_fnames:
                skipped_in_chat += 1
                continue

            scope = index.innermost_scope(tag.rel_fname, tag.line)
            scope_name = index.qualified_name(scope) if scope else ""
            hits.append(Hit(tag.rel_fname, tag.line, "call", scope_name, ""))

        return hits, total, skipped_in_chat

    def render_callees(self, index, name, def_scopes, chat_rel_fnames):
        """What the definition body itself calls, resolved to where they live."""

        callees = dict()

        for scope in def_scopes:
            for tag in index.refs_in_range(scope.rel_fname, scope.start_line, scope.end_line):
                if tag.name == name:
                    continue
                if tag.name in callees:
                    continue
                if self.is_noisy(index, tag.name):
                    continue

                targets = index.defs.get(tag.name) or []
                targets = [
                    target
                    for target in targets
                    if not (target.rel_fname == scope.rel_fname and target.line == tag.line)
                ]
                if not targets:
                    continue

                # Prefer a definition in the same file, then by file rank
                targets = sorted(
                    targets,
                    key=lambda target: (
                        target.rel_fname != scope.rel_fname,
                    )
                    + self.file_sort_key(target.rel_fname),
                )
                callees[tag.name] = targets

        if not callees:
            return f"\n`{name}` does not call anything else that is defined in this repo.", set()

        lines = [f"\n`{name}` uses these, defined elsewhere in the repo:"]
        files = set()

        ordered = sorted(
            callees.items(),
            key=lambda item: self.file_sort_key(item[1][0].rel_fname) + (item[0],),
        )

        for callee_name, targets in ordered[:MAX_CALLEES]:
            target = targets[0]
            files.add(target.rel_fname)
            suffix = ""
            if len(targets) > 1:
                suffix = f" ({len(targets)} definitions)"
            in_chat = " [in chat]" if target.rel_fname in chat_rel_fnames else ""
            lines.append(
                f"- {callee_name} -> {target.rel_fname}:{target.line + 1}{suffix}{in_chat}"
            )

        if len(ordered) > MAX_CALLEES:
            lines.append(f"- ...and {len(ordered) - MAX_CALLEES} more")

        return "\n".join(lines), files

    def is_noisy(self, index, ident):
        """Mirror the repo map's own heuristics for uninteresting identifiers."""

        if len(ident) < 3:
            return True
        if ident.startswith("__"):
            return True
        if len(index.defs.get(ident, [])) > 5:
            return True
        return False

    ##
    # Variables / attributes / fields
    ##

    def trace_data(self, index, req, name, defs, chat_rel_fnames, max_tokens):
        attribute = self.looks_like_attribute(index, req, name, defs)

        scan_files = self.scan_files(index, req, name, defs)
        hits, total, skipped_in_chat, file_counts = self.find_occurrences(
            index, name, scan_files, chat_rel_fnames, attribute=attribute
        )

        if not hits and not total:
            lines = [f"\nNo uses of `{req.symbol}` found in the repo."]
            lines.append(self.suggest_similar(index, name))
            return "\n".join(line for line in lines if line)

        if len(file_counts) > MAX_FILES_BEFORE_AMBIGUOUS:
            return self.render_ambiguous_files(req, name, total, file_counts)

        kind_label = "attribute" if attribute else "variable"
        def_scopes = [index.scope_for_def(tag) for tag in defs]
        def_scopes = [scope for scope in def_scopes if scope]

        sections = [self.describe_symbol(index, req.symbol, kind_label, def_scopes)]

        writes = [hit for hit in hits if hit.kind in ("write", "param")]
        reads = [hit for hit in hits if hit.kind not in ("write", "param")]

        shown_files = set()

        if req.direction in ("up", "both"):
            text, files = self.render_hits(
                index,
                writes,
                len(writes),
                max_tokens * (0.5 if req.direction == "both" else 1.0),
                title=f"Where `{name}` is set (assignments, parameters)",
                empty=f"No assignments to `{name}` found.",
                skipped_in_chat=skipped_in_chat if req.direction == "up" else 0,
            )
            shown_files |= files
            sections.append(text)

        if req.direction in ("down", "both"):
            text, files = self.render_hits(
                index,
                reads,
                len(reads),
                max_tokens * (0.5 if req.direction == "both" else 1.0),
                title=f"Where `{name}` is read",
                empty=f"No reads of `{name}` found.",
                skipped_in_chat=skipped_in_chat,
            )
            shown_files |= files
            sections.append(text)

        flows = self.render_flows(hits)
        if flows:
            sections.append(flows)

        footer = self.render_footer(shown_files, chat_rel_fnames)
        if footer:
            sections.append(footer)

        return "\n".join(section for section in sections if section)

    def looks_like_attribute(self, index, req, name, defs):
        raw = req.symbol.lower()
        if raw.startswith(("self.", "this.", "cls.")):
            return True

        for tag in defs:
            scope = index.scope_for_def(tag)
            if scope and scope.kind in ("field", "property"):
                return True

        return False

    def scan_files(self, index, req, name, defs):
        """Which files to search for a data symbol."""

        if req.file:
            wanted = req.file.replace("\\", "/")
            matches = [
                rel
                for rel in index.abs_fnames
                if rel.replace("\\", "/").endswith(wanted) or rel.replace("\\", "/") == wanted
            ]
            if matches:
                return matches

        return list(index.abs_fnames)

    def find_occurrences(self, index, name, rel_fnames, chat_rel_fnames, attribute=False):
        """Word-boundary scan, classified read/write via tree-sitter."""

        if attribute:
            pattern = re.compile(r"(?<=\.)" + re.escape(name) + r"\b")
        else:
            pattern = re.compile(r"\b" + re.escape(name) + r"\b")

        hits = []
        total = 0
        skipped_in_chat = 0
        file_counts = defaultdict(int)

        for rel_fname in sorted(rel_fnames):
            abs_fname = index.abs_fnames.get(rel_fname)
            if not abs_fname:
                continue
            if not filename_to_lang(abs_fname):
                continue

            text = self.read_text(abs_fname)
            if not text or name not in text:
                continue

            lang, tree = self.get_tree(abs_fname)

            lines = text.splitlines()
            for lineno, line in enumerate(lines):
                for match in pattern.finditer(line):
                    if self.in_comment_or_string(line, match.start()):
                        continue

                    total += 1
                    file_counts[rel_fname] += 1

                    if rel_fname in chat_rel_fnames:
                        skipped_in_chat += 1
                        continue

                    kind, note = self.classify(
                        index, lang, tree, lineno, match.start(), name, line
                    )
                    scope = index.innermost_scope(rel_fname, lineno)
                    scope_name = index.qualified_name(scope) if scope else ""
                    hits.append(Hit(rel_fname, lineno, kind, scope_name, note))

        return hits, total, skipped_in_chat, file_counts

    def in_comment_or_string(self, line, col):
        """Cheap filter for matches inside a line comment."""

        before = line[:col]
        for marker in ("#", "//"):
            idx = before.find(marker)
            if idx >= 0 and before.count('"', 0, idx) % 2 == 0:
                return True
        return False

    def classify(self, index, lang, tree, row, col, name, line):
        """Classify an occurrence as write/param/read, with a flow note."""

        node = None
        if tree is not None:
            try:
                node = tree.root_node.descendant_for_point_range((row, col), (row, col + len(name)))
            except Exception:
                node = None

        if node is None:
            return self.classify_by_text(name, line), ""

        try:
            if lang == "python":
                return self.classify_python(index, node, name)
            if lang in ("javascript", "typescript", "tsx", "jsx"):
                return self.classify_js(index, node, name)
        except Exception as err:
            if self.verbose:
                self.io.tool_warning(f"Unable to classify use of {name}: {err}")

        return self.classify_by_text(name, line), ""

    def classify_by_text(self, name, line):
        if re.search(r"\b" + re.escape(name) + r"\s*(?::[^=]*)?=(?!=)", line):
            return "write"
        return "read"

    def field_contains(self, parent, field, node):
        try:
            target = parent.child_by_field_name(field)
        except Exception:
            return False

        while node is not None:
            if target is not None and node.id == target.id:
                return True
            node = node.parent
            if parent is not None and node is not None and node.id == parent.id:
                break

        return False

    def classify_python(self, index, node, name):
        current = node
        # Step out of attribute/subscript wrappers so `self.x = 1` counts as a write
        while current.parent is not None and current.parent.type in (
            "attribute",
            "subscript",
            "pattern_list",
            "tuple_pattern",
            "list_pattern",
        ):
            current = current.parent

        parent = current.parent
        if parent is None:
            return "read", ""

        ptype = parent.type

        if ptype == "assignment":
            if self.field_contains(parent, "left", current):
                return "write", self.value_source_note(index, parent)
            return "read", ""

        if ptype == "augmented_assignment":
            if self.field_contains(parent, "left", current):
                return "write", ""
            return "read", ""

        if ptype in ("parameters", "lambda_parameters", "default_parameter", "typed_parameter",
                     "typed_default_parameter"):
            return "param", ""

        if ptype in ("for_statement", "for_in_clause"):
            if self.field_contains(parent, "left", current):
                return "write", ""
            return "read", ""

        if ptype in ("as_pattern", "as_pattern_target"):
            return "write", ""

        if ptype in ("global_statement", "nonlocal_statement"):
            return "write", ""

        if ptype in ("import_statement", "import_from_statement", "aliased_import", "dotted_name"):
            return "read", ""

        if ptype == "keyword_argument" and self.field_contains(parent, "name", current):
            return "param", ""

        if ptype == "return_statement":
            return "read", "returned from the enclosing function"

        return "read", self.argument_note(index, parent, current)

    def classify_js(self, index, node, name):
        current = node
        while current.parent is not None and current.parent.type in (
            "member_expression",
            "subscript_expression",
        ):
            current = current.parent

        parent = current.parent
        if parent is None:
            return "read", ""

        ptype = parent.type

        if ptype == "variable_declarator" and self.field_contains(parent, "name", current):
            return "write", self.value_source_note(index, parent)

        if ptype in ("assignment_expression", "augmented_assignment_expression"):
            if self.field_contains(parent, "left", current):
                return "write", self.value_source_note(index, parent)
            return "read", ""

        if ptype in ("formal_parameters", "required_parameter", "optional_parameter"):
            return "param", ""

        if ptype == "return_statement":
            return "read", "returned from the enclosing function"

        return "read", self.argument_note(index, parent, current)

    def where_defined(self, index, callee):
        """`file:line` for a name defined in this repo, or None."""

        targets = index.defs.get(callee)
        if not targets:
            return None

        target = sorted(targets, key=lambda tag: self.file_sort_key(tag.rel_fname))[0]
        return f"{target.rel_fname}:{target.line + 1}"

    def value_source_note(self, index, assignment_node):
        """`x = foo(...)` -> note that the value comes from foo()."""

        try:
            value = assignment_node.child_by_field_name("value")
        except Exception:
            return ""

        call_types = ("call", "call_expression", "await", "new_expression")
        if value is None or value.type not in call_types:
            return ""

        if value.type == "await" and value.named_child_count:
            value = value.named_children[0]

        try:
            func = value.child_by_field_name("function")
        except Exception:
            return ""

        if func is None:
            return ""

        callee = func.text.decode("utf-8", errors="replace").split(".")[-1]
        if not callee:
            return ""

        # Only worth reporting if we can say where the value is produced
        where = self.where_defined(index, callee)
        if not where:
            return ""

        return f"value comes from {callee}() at {where}"

    def argument_note(self, index, parent, current):
        """`foo(x)` -> note which parameter of foo it lands in."""

        if parent.type not in ("argument_list", "arguments"):
            return ""

        call = parent.parent
        if call is None or call.type not in ("call", "call_expression", "new_expression"):
            return ""

        try:
            func = call.child_by_field_name("function")
        except Exception:
            return ""

        if func is None:
            return ""

        callee = func.text.decode("utf-8", errors="replace").split(".")[-1]
        if not callee:
            return ""

        where = self.where_defined(index, callee)
        if not where:
            return ""

        position = 0
        for child in parent.named_children:
            if child.id == current.id:
                break
            position += 1

        return f"passed to {callee}() as argument {position + 1}, defined at {where}"

    def render_flows(self, hits):
        notes = []
        seen = set()
        for hit in hits:
            if not hit.note:
                continue
            entry = f"- {hit.rel_fname}:{hit.line + 1}: {hit.note}"
            if entry in seen:
                continue
            seen.add(entry)
            notes.append(entry)

        if not notes:
            return ""

        return "\nHow the value flows:\n" + "\n".join(notes[:MAX_CALLEES])

    ##
    # Rendering
    ##

    def describe_symbol(self, index, symbol, kind_label, def_scopes):
        if def_scopes:
            where = ", ".join(
                f"{scope.rel_fname}:{scope.start_line + 1}" for scope in def_scopes[:3]
            )
            return f"\nTrace of `{symbol}` ({kind_label}, defined at {where}):"

        return f"\nTrace of `{symbol}` ({kind_label}, no definition found in the repo map):"

    def render_definitions(self, index, def_scopes):
        parts = []
        for scope in def_scopes[:3]:
            abs_fname = index.abs_fnames.get(scope.rel_fname)
            if not abs_fname:
                continue
            body = self.render_tree(abs_fname, scope.rel_fname, [scope.start_line])
            if body:
                parts.append(f"\n{scope.rel_fname}:\n{body}")

        if not parts:
            return ""

        return "\nDefinition:" + "".join(parts)

    def render_hits(self, index, hits, total, budget, title, empty, skipped_in_chat=0):
        """Render hits grouped by file, richest-signal file first, within budget."""

        if not hits:
            text = f"\n{empty}"
            if skipped_in_chat:
                text += f" ({skipped_in_chat} more are in files already in the chat.)"
            return text, set()

        by_file = defaultdict(list)
        for hit in hits:
            by_file[hit.rel_fname].append(hit)

        ordered = sorted(
            by_file.items(),
            key=lambda item: self.file_sort_key(item[0]) + (len(item[1]),),
        )

        shown_files = set()
        shown_hits = 0
        parts = []
        used = 0

        for rel_fname, file_hits in ordered:
            abs_fname = index.abs_fnames.get(rel_fname)
            if not abs_fname:
                continue

            lois = self.pick_lois(file_hits)
            body = self.render_tree(abs_fname, rel_fname, lois)
            if not body:
                continue

            chunk = f"\n{rel_fname}:\n{body}"
            chunk_tokens = self.token_count(chunk)

            if parts and used + chunk_tokens > budget:
                break

            parts.append(chunk)
            used += chunk_tokens
            shown_files.add(rel_fname)
            shown_hits += len(lois)

            if used > budget:
                break

        header = f"\n{title} ({total} found"
        if shown_hits < total:
            header += f", showing {shown_hits}"
        if skipped_in_chat:
            header += f", {skipped_in_chat} in files already in the chat"
        header += "):"

        remaining = [rel for rel, _ in ordered if rel not in shown_files]
        tail = ""
        if remaining:
            listed = ", ".join(remaining[:6])
            more = "" if len(remaining) <= 6 else f" and {len(remaining) - 6} other files"
            tail = f"\nAlso used in: {listed}{more}"

        return header + "".join(parts) + tail, shown_files

    def pick_lois(self, file_hits):
        """Spread the shown lines across enclosing functions, not all in one."""

        per_scope = defaultdict(int)
        lois = []

        for hit in sorted(file_hits, key=lambda hit: hit.line):
            if per_scope[hit.scope_name] >= MAX_LOIS_PER_SCOPE:
                continue
            per_scope[hit.scope_name] += 1
            lois.append(hit.line)
            if len(lois) >= MAX_LOIS_PER_FILE:
                break

        return lois

    def render_tree(self, abs_fname, rel_fname, lois):
        """Render lines of interest with just enough enclosing scope to read them.

        Same idea as the repo map's rendering, but with line numbers (so the
        model can point back at a location) and short scope headers, since a
        call site is only useful together with the function it sits in.
        """

        if not lois:
            return ""

        try:
            mtime = os.path.getmtime(abs_fname)
        except OSError:
            return ""

        cached = self.snippet_context_cache.get(rel_fname)
        if not cached or cached["mtime"] != mtime:
            code = self.read_text(abs_fname)
            if not code:
                return ""
            if not code.endswith("\n"):
                code += "\n"

            try:
                context = TreeContext(
                    rel_fname,
                    code,
                    color=False,
                    line_number=True,
                    child_context=False,
                    last_line=False,
                    margin=0,
                    mark_lois=False,
                    loi_pad=0,
                    header_max=3,
                    show_top_of_file_parent_scope=False,
                )
            except Exception as err:
                if self.verbose:
                    self.io.tool_warning(f"Unable to render {rel_fname}: {err}")
                return ""

            if len(self.snippet_context_cache) > 50:
                self.snippet_context_cache.clear()
            cached = {"context": context, "mtime": mtime}
            self.snippet_context_cache[rel_fname] = cached

        context = cached["context"]
        try:
            context.lines_of_interest = set()
            context.add_lines_of_interest(lois)
            context.add_context()
            body = context.format()
        except Exception as err:
            if self.verbose:
                self.io.tool_warning(f"Unable to render {rel_fname}: {err}")
            return ""

        # Same truncation the repo map applies, in case of minified sources
        return "\n".join(line[:100] for line in body.splitlines())

    def render_footer(self, files, chat_rel_fnames):
        addable = sorted(rel for rel in files if rel not in chat_rel_fnames)
        if not addable:
            return ""

        return (
            "\nNone of these files are in the chat."
            " Ask me to *add* only the ones you actually need to see or edit."
        )

    def render_ambiguous_defs(self, index, req, name, defs):
        lines = [
            f"\n`{req.symbol}` is defined in {len(defs)} places, which is too many to trace"
            " usefully. Pick one and trace it as `Container.name` or `path/to/file.py:name`:"
        ]

        ordered = sorted(defs, key=lambda tag: (-self.file_rank(tag.rel_fname), tag.rel_fname))
        for tag in ordered[:10]:
            scope = index.scope_for_def(tag)
            qualified = index.qualified_name(scope) if scope else tag.name
            lines.append(f"- {tag.rel_fname}:{tag.line + 1} `{qualified}`")

        if len(ordered) > 10:
            lines.append(f"- ...and {len(ordered) - 10} more")

        return "\n".join(lines)

    def render_ambiguous_files(self, req, name, total, file_counts):
        lines = [
            f"\n`{req.symbol}` appears {total} times across {len(file_counts)} files, which is"
            " too common to trace usefully. Narrow it down with"
            f" `{name} in path/to/file.py` or `EnclosingFunction.{name}`. Most uses are in:"
        ]

        ordered = sorted(file_counts.items(), key=lambda item: (-item[1], item[0]))
        for rel_fname, count in ordered[:10]:
            lines.append(f"- {rel_fname} ({count} uses)")

        return "\n".join(lines)

    def suggest_similar(self, index, name):
        matches = get_close_matches(name, index.def_names(), n=5, cutoff=0.7)
        if not matches:
            return ""

        return "\nSimilar names that do exist: " + ", ".join(f"`{m}`" for m in matches)

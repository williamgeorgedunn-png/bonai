import hashlib
import re
from pathlib import Path


class Fact:
    """One answer to a NEED request, ready to paste into a prompt."""

    def __init__(self, need, title, text, tokens=0):
        self.need = need
        self.title = title
        self.text = text
        self.tokens = tokens

    def render(self):
        return f"## {self.title}\n{self.text}".strip()


class KnowledgeService:
    """Answers the architect's NEED requests, cheapest source first.

    Tier 0 is the repo map (always present). Tier 1 is exact static lookup
    from the repo map's symbol index and plain search - free, no LLM. Tier 2
    is a worker-written digest, cached on disk by content hash. Tier 3 is raw
    source, which costs the most architect context and is used last.
    """

    def __init__(
        self,
        root,
        io,
        config,
        repo_map=None,
        tracer=None,
        token_count=None,
        digest_fn=None,
        get_all_abs_files=None,
        cache_dir=None,
    ):
        self.root = Path(root)
        self.io = io
        self.config = config
        self.repo_map = repo_map
        self.tracer = tracer
        self.token_count = token_count or (lambda text: max(1, len(text) // 4))
        self.digest_fn = digest_fn
        self.get_all_abs_files = get_all_abs_files or (lambda: [])
        self.cache_dir = Path(cache_dir) if cache_dir else self.root / ".aider.pipeline" / "digests"
        self._index = None
        self._index_key = None
        self.digest_calls = 0
        self.digest_hits = 0

    # --------------------------------------------------------------- index

    def index(self):
        """The repo map's symbol index, rebuilt only when files change."""
        if not self.tracer:
            return None
        fnames = list(self.get_all_abs_files())
        key = len(fnames)
        if self._index is None or self._index_key != key:
            self._index = self.tracer.get_index(fnames)
            self._index_key = key
        return self._index

    def abs_path(self, rel_fname):
        return str(self.root / rel_fname)

    def read(self, rel_fname):
        text = self.io.read_text(self.abs_path(rel_fname))
        return text or ""

    # -------------------------------------------------------------- facts

    def resolve(self, needs, budget=None):
        """Answer needs in order until the token budget is spent.

        Returns (facts, omitted_count).
        """
        budget = self.config.facts_tokens if budget is None else budget
        facts = []
        used = 0
        omitted = 0
        for need in needs:
            if used >= budget:
                omitted += 1
                continue
            try:
                fact = self.answer(need)
            except Exception as err:  # a bad lookup must not kill the run
                fact = Fact(need, f"{need.kind} {need.target or need.symbol}", f"Lookup failed: {err}")
            if fact is None:
                continue
            fact.tokens = self.token_count(fact.text)
            if used + fact.tokens > budget:
                remaining = max(0, budget - used)
                if remaining < 40:
                    omitted += 1
                    continue
                fact.text = self.clamp(fact.text, remaining)
                fact.text += "\n(truncated: ask more narrowly)"
                fact.tokens = self.token_count(fact.text)
            facts.append(fact)
            used += fact.tokens
        return facts, omitted

    def answer(self, need):
        handler = getattr(self, f"_need_{need.kind}", None)
        if handler is None:
            return None
        return handler(need)

    def clamp(self, text, max_tokens):
        lines = text.splitlines()
        while lines and self.token_count("\n".join(lines)) > max_tokens:
            drop = max(1, len(lines) // 10)
            lines = lines[:-drop]
        return "\n".join(lines)

    # ------------------------------------------------------------ tier 1

    def _need_outline(self, need):
        index = self.index()
        title = f"Outline of {need.target}"
        if index is None:
            return Fact(need, title, "No symbol index available.")

        scopes = index.scopes.get(need.target)
        if not scopes:
            match = self._match_file(need.target)
            if match:
                scopes = index.scopes.get(match)
                title = f"Outline of {match}"
                need.target = match
        if not scopes:
            return Fact(need, title, f"No definitions found in {need.target}.")

        source = self.read(need.target).splitlines()
        lines = []
        for scope in sorted(scopes, key=lambda s: s.start_line):
            name = index.qualified_name(scope)
            signature = ""
            if 0 <= scope.start_line < len(source):
                signature = source[scope.start_line].strip()
            span = f"L{scope.start_line + 1}-L{scope.end_line + 1}"
            lines.append(f"- {name} ({scope.kind}, {span}): {signature}")
        return Fact(need, title, "\n".join(lines))

    def _need_refs(self, need):
        index = self.index()
        title = f"References to {need.symbol}"
        if index is None:
            return Fact(need, title, "No symbol index available.")

        tags = index.refs.get(need.symbol) or []
        if not tags:
            return self._grep_fact(
                need,
                title,
                rf"\b{re.escape(need.symbol)}\b",
                note="(no indexed references; showing text matches)",
            )

        by_file = {}
        for tag in tags:
            by_file.setdefault(tag.rel_fname, []).append(tag.line + 1)

        lines = []
        shown = 0
        for rel_fname in sorted(by_file):
            hits = sorted(set(by_file[rel_fname]))
            if shown >= self.config.grep_hits:
                lines.append(f"(more files omitted: {len(by_file) - len(lines)})")
                break
            lines.append(f"- {rel_fname}: lines {', '.join(str(h) for h in hits[:12])}")
            shown += len(hits[:12])

        defs = index.defs.get(need.symbol) or []
        if defs:
            where = ", ".join(f"{t.rel_fname}:{t.line + 1}" for t in defs[:4])
            lines.insert(0, f"Defined at: {where}")
        return Fact(need, title, "\n".join(lines))

    def _need_grep(self, need):
        return self._grep_fact(need, f"Search for {need.target}", need.target)

    def _grep_fact(self, need, title, pattern, note=""):
        try:
            regex = re.compile(pattern)
        except re.error as err:
            return Fact(need, title, f"Invalid pattern: {err}")

        hits = []
        for abs_fname in sorted(self.get_all_abs_files()):
            if len(hits) >= self.config.grep_hits:
                break
            text = self.io.read_text(abs_fname)
            if not text:
                continue
            rel = self._rel(abs_fname)
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"- {rel}:{number}: {line.strip()[:160]}")
                    if len(hits) >= self.config.grep_hits:
                        break

        if not hits:
            return Fact(need, title, "No matches.")
        body = "\n".join(hits)
        if note:
            body = f"{note}\n{body}"
        if len(hits) >= self.config.grep_hits:
            body += f"\n(stopped at {self.config.grep_hits} matches)"
        return Fact(need, title, body)

    def _need_trace(self, need):
        title = f"Trace of {need.symbol}"
        if not self.tracer:
            return Fact(need, title, "Tracing is not available.")
        from aider.tracer import TraceRequest

        req = TraceRequest(
            symbol=need.symbol, direction="both", depth=1, file=None, strict=True
        )
        text = self.tracer.trace(
            req,
            list(self.get_all_abs_files()),
            chat_rel_fnames=(),
            max_tokens=min(self.config.facts_tokens, 1200),
        )
        return Fact(need, title, text or "No trace results.")

    def _need_about(self, need):
        """Convenience: definitions and references, plus a digest if cheap."""
        index = self.index()
        title = f"About {need.symbol}"
        parts = []

        refs = self._need_refs(need)
        if refs:
            parts.append(refs.text)

        if index is not None and self.digest_fn:
            defs = index.defs.get(need.symbol) or []
            if len(defs) == 1:
                scope = index.scope_for_def(defs[0])
                if scope:
                    digest = self._digest_scope(defs[0].rel_fname, need.symbol, scope)
                    if digest:
                        parts.append(f"Digest: {digest}")

        return Fact(need, title, "\n".join(parts) or "Nothing found.")

    # ------------------------------------------------------------ tier 2

    def _need_digest(self, need):
        title = f"Digest of {need.target}" + (f"::{need.symbol}" if need.symbol else "")
        rel_fname = self._match_file(need.target) or need.target
        text, label = self._slice(rel_fname, need)
        if not text:
            return Fact(need, title, f"Could not read {need.target}.")
        digest = self._digest_text(text, label)
        return Fact(need, title, digest or "Digest unavailable.")

    def _digest_scope(self, rel_fname, symbol, scope):
        source = self.read(rel_fname).splitlines()
        text = "\n".join(source[scope.start_line : scope.end_line + 1])
        return self._digest_text(text, f"{rel_fname}::{symbol}")

    def _digest_text(self, text, label):
        if not self.digest_fn:
            return None
        key = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]
        cache_file = self.cache_dir / f"{key}.md"
        if cache_file.exists():
            self.digest_hits += 1
            return cache_file.read_text(encoding="utf-8").strip()

        digest = self.digest_fn(text, label)
        if not digest:
            return None
        digest = digest.strip()
        self.digest_calls += 1
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(digest, encoding="utf-8")
        except OSError:
            pass
        return digest

    # ------------------------------------------------------------ tier 3

    def _need_source(self, need):
        rel_fname = self._match_file(need.target) or need.target
        label = f"{rel_fname}"
        text, label = self._slice(rel_fname, need)
        if not text:
            return Fact(need, f"Source of {need.target}", f"Could not read {need.target}.")
        text = self.clamp(text, self.config.source_slice_tokens)
        lang = Path(rel_fname).suffix.lstrip(".") or ""
        return Fact(need, f"Source of {label}", f"```{lang}\n{text}\n```")

    def _slice(self, rel_fname, need):
        """Return (text, label) for a file, symbol or line range."""
        source = self.read(rel_fname)
        if not source:
            return "", rel_fname
        lines = source.splitlines()

        if need.start_line and need.end_line:
            start = max(0, need.start_line - 1)
            end = min(len(lines), need.end_line)
            return "\n".join(lines[start:end]), f"{rel_fname}:L{start + 1}-L{end}"

        if need.symbol:
            span = self.symbol_range(rel_fname, need.symbol)
            if span:
                start, end = span
                body = "\n".join(lines[start : end + 1])
                return body, f"{rel_fname}::{need.symbol} (L{start + 1}-L{end + 1})"
            return "", f"{rel_fname}::{need.symbol}"

        return source, rel_fname

    def symbol_range(self, rel_fname, symbol):
        """0-based inclusive line range of a definition, or None."""
        index = self.index()
        if index is None:
            return None
        scopes = index.scopes.get(rel_fname) or []
        wanted = symbol.split(".")[-1]
        for scope in scopes:
            if scope.name == symbol or index.qualified_name(scope) == symbol:
                return scope.start_line, scope.end_line
        for scope in scopes:
            if scope.name == wanted:
                return scope.start_line, scope.end_line
        return None

    # ------------------------------------------------------------- helpers

    def _rel(self, abs_fname):
        try:
            return str(Path(abs_fname).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(abs_fname).replace("\\", "/")

    def _match_file(self, target):
        """Map a possibly partial path onto a real repo file."""
        if not target:
            return None
        target = target.replace("\\", "/")
        rels = [self._rel(f) for f in self.get_all_abs_files()]
        if target in rels:
            return target
        suffix_matches = [r for r in rels if r.endswith("/" + target)]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        base_matches = [r for r in rels if Path(r).name == Path(target).name]
        if len(base_matches) == 1:
            return base_matches[0]
        return None

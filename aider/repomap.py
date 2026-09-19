import colorsys
import math
import os
import random
import shutil
import sqlite3
import sys
import time
import warnings
from collections import Counter, defaultdict, namedtuple
from importlib import resources
from pathlib import Path

from diskcache import Cache
from grep_ast import TreeContext, filename_to_lang
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from tqdm import tqdm
from tree_sitter import Query

from aider.dump import dump
from aider.special import filter_important_files
from aider.waiting import Spinner

# tree_sitter is throwing a FutureWarning
warnings.simplefilter("ignore", category=FutureWarning)
from grep_ast.tsl import USING_TSL_PACK, get_language, get_parser  # noqa: E402

Tag = namedtuple("Tag", "rel_fname fname line name kind".split())

# The source range of a definition (function, class, method, ...), used to
# answer "which definition encloses line N" and to extract snippets.
Scope = namedtuple("Scope", "rel_fname fname name kind start_line end_line".split())

# Node types that enclose a definition, used when a language's tags query
# doesn't capture the definition node itself.
SCOPE_NODE_SUFFIXES = (
    "_definition",
    "_declaration",
    "_specifier",
    "_item",
    "_statement",
    "_method",
    "_block",
)

SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError, OSError)


CACHE_VERSION = 4
if USING_TSL_PACK:
    CACHE_VERSION = 5

UPDATING_REPO_MAP_MESSAGE = "Updating repo map"


class RepoMap:
    TAGS_CACHE_DIR = f".aider.tags.cache.v{CACHE_VERSION}"

    warned_files = set()

    def __init__(
        self,
        map_tokens=1024,
        root=None,
        main_model=None,
        io=None,
        repo_content_prefix=None,
        verbose=False,
        max_context_window=None,
        map_mul_no_files=8,
        refresh="auto",
    ):
        self.io = io
        self.verbose = verbose
        self.refresh = refresh

        if not root:
            root = os.getcwd()
        self.root = root

        self.load_tags_cache()
        self.cache_threshold = 0.95

        self.max_map_tokens = map_tokens
        self.map_mul_no_files = map_mul_no_files
        self.max_context_window = max_context_window

        self.repo_content_prefix = repo_content_prefix

        self.main_model = main_model

        self.tree_cache = {}
        self.tree_context_cache = {}
        self.map_cache = {}
        self.map_processing_time = 0
        self.last_map = None

        # Populated by get_ranked_tags(), reused to rank trace results
        self.last_ranked = {}

        self.symbol_index = None
        self.symbol_index_key = None

        if self.verbose:
            self.io.tool_output(
                f"RepoMap initialized with map_mul_no_files: {self.map_mul_no_files}"
            )

    def token_count(self, text):
        len_text = len(text)
        if len_text < 200:
            return self.main_model.token_count(text)

        lines = text.splitlines(keepends=True)
        num_lines = len(lines)
        step = num_lines // 100 or 1
        lines = lines[::step]
        sample_text = "".join(lines)
        sample_tokens = self.main_model.token_count(sample_text)
        est_tokens = sample_tokens / len(sample_text) * len_text
        return est_tokens

    def get_repo_map(
        self,
        chat_files,
        other_files,
        mentioned_fnames=None,
        mentioned_idents=None,
        force_refresh=False,
    ):
        if self.max_map_tokens <= 0:
            return
        if not other_files:
            return
        if not mentioned_fnames:
            mentioned_fnames = set()
        if not mentioned_idents:
            mentioned_idents = set()

        max_map_tokens = self.max_map_tokens

        # With no files in the chat, give a bigger view of the entire repo
        padding = 4096
        if max_map_tokens and self.max_context_window:
            target = min(
                int(max_map_tokens * self.map_mul_no_files),
                self.max_context_window - padding,
            )
        else:
            target = 0
        if not chat_files and self.max_context_window and target > 0:
            max_map_tokens = target

        try:
            files_listing = self.get_ranked_tags_map(
                chat_files,
                other_files,
                max_map_tokens,
                mentioned_fnames,
                mentioned_idents,
                force_refresh,
            )
        except RecursionError:
            self.io.tool_error("Disabling repo map, git repo too large?")
            self.max_map_tokens = 0
            return

        if not files_listing:
            return

        if self.verbose:
            num_tokens = self.token_count(files_listing)
            self.io.tool_output(f"Repo-map: {num_tokens / 1024:.1f} k-tokens")

        if chat_files:
            other = "other "
        else:
            other = ""

        if self.repo_content_prefix:
            repo_content = self.repo_content_prefix.format(other=other)
        else:
            repo_content = ""

        repo_content += files_listing

        return repo_content

    def get_rel_fname(self, fname):
        try:
            return os.path.relpath(fname, self.root)
        except ValueError:
            # Issue #1288: ValueError: path is on mount 'C:', start on mount 'D:'
            # Just return the full fname.
            return fname

    def tags_cache_error(self, original_error=None):
        """Handle SQLite errors by trying to recreate cache, falling back to dict if needed"""

        if self.verbose and original_error:
            self.io.tool_warning(f"Tags cache error: {str(original_error)}")

        if isinstance(getattr(self, "TAGS_CACHE", None), dict):
            return

        path = Path(self.root) / self.TAGS_CACHE_DIR

        # Try to recreate the cache
        try:
            # Delete existing cache dir
            if path.exists():
                shutil.rmtree(path)

            # Try to create new cache
            new_cache = Cache(path)

            # Test that it works
            test_key = "test"
            new_cache[test_key] = "test"
            _ = new_cache[test_key]
            del new_cache[test_key]

            # If we got here, the new cache works
            self.TAGS_CACHE = new_cache
            return

        except SQLITE_ERRORS as e:
            # If anything goes wrong, warn and fall back to dict
            self.io.tool_warning(
                f"Unable to use tags cache at {path}, falling back to memory cache"
            )
            if self.verbose:
                self.io.tool_warning(f"Cache recreation error: {str(e)}")

        self.TAGS_CACHE = dict()

    def load_tags_cache(self):
        path = Path(self.root) / self.TAGS_CACHE_DIR
        try:
            self.TAGS_CACHE = Cache(path)
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)

    def save_tags_cache(self):
        pass

    def get_mtime(self, fname):
        try:
            return os.path.getmtime(fname)
        except FileNotFoundError:
            self.io.tool_warning(f"File not found error: {fname}")

    def get_tags(self, fname, rel_fname):
        # Check if the file is in the cache and if the modification time has not changed
        file_mtime = self.get_mtime(fname)
        if file_mtime is None:
            return []

        cache_key = fname
        try:
            val = self.TAGS_CACHE.get(cache_key)  # Issue #1308
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            val = self.TAGS_CACHE.get(cache_key)

        if val is not None and val.get("mtime") == file_mtime and "scopes" in val:
            try:
                return self.TAGS_CACHE[cache_key]["data"]
            except SQLITE_ERRORS as e:
                self.tags_cache_error(e)
                return self.TAGS_CACHE[cache_key]["data"]

        # miss!
        data, scopes = self.get_tags_and_scopes_raw(fname, rel_fname)

        # Update the cache
        entry = {"mtime": file_mtime, "data": data, "scopes": scopes}
        try:
            self.TAGS_CACHE[cache_key] = entry
            self.save_tags_cache()
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            self.TAGS_CACHE[cache_key] = entry

        return data

    def get_tags_and_scopes(self, fname, rel_fname):
        """Tags and definition ranges, from a single cache lookup."""

        # Populate/refresh the cache entry
        tags = self.get_tags(fname, rel_fname)

        try:
            val = self.TAGS_CACHE.get(fname)
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            val = self.TAGS_CACHE.get(fname)

        scopes = val.get("scopes") if val else None

        return tags or [], scopes or []

    def get_scopes(self, fname, rel_fname):
        """Definition ranges for a file, in the same cache entry as its tags."""

        _tags, scopes = self.get_tags_and_scopes(fname, rel_fname)
        return scopes

    def _run_captures(self, query: Query, node):
        # tree-sitter 0.23.2's python bindings had captures directly on the Query object
        # but 0.24.0 moved it to a separate QueryCursor class. Support both.
        if hasattr(query, "captures"):
            # Old API
            return query.captures(node)

        # New API
        from tree_sitter import QueryCursor

        cursor = QueryCursor(query)
        return cursor.captures(node)

    def get_tags_raw(self, fname, rel_fname):
        tags, _scopes = self.get_tags_and_scopes_raw(fname, rel_fname)
        return tags

    def get_definition_range(self, name_node, def_node_ids):
        """Line range of the definition that `name_node` names."""

        node = name_node
        while node is not None:
            if node.id in def_node_ids:
                return node.start_point[0], node.end_point[0]
            node = node.parent

        # Languages whose tags query doesn't capture the definition node
        node = name_node.parent
        while node is not None:
            if node.type.endswith(SCOPE_NODE_SUFFIXES) and node.end_point[0] > node.start_point[0]:
                return node.start_point[0], node.end_point[0]
            node = node.parent

        return name_node.start_point[0], name_node.end_point[0]

    def get_tags_and_scopes_raw(self, fname, rel_fname):
        lang = filename_to_lang(fname)
        if not lang:
            return [], []

        try:
            language = get_language(lang)
            parser = get_parser(lang)
        except Exception as err:
            print(f"Skipping file {fname}: {err}")
            return [], []

        query_scm = get_scm_fname(lang)
        if not query_scm.exists():
            return [], []
        query_scm = query_scm.read_text()

        code = self.io.read_text(fname)
        if not code:
            return [], []
        tree = parser.parse(bytes(code, "utf-8"))

        # Run the tags queries
        captures = self._run_captures(Query(language, query_scm), tree.root_node)

        captures_by_tag = defaultdict(list)
        matches = []
        for tag, nodes in captures.items():
            for node in nodes:
                captures_by_tag[tag].append(node)
            captures_by_tag[tag].append(node)
            matches.append((node, tag))

        if USING_TSL_PACK:
            all_nodes = [(node, tag) for tag, nodes in captures_by_tag.items() for node in nodes]
        else:
            all_nodes = matches

        def_node_ids = set()
        for tag, nodes in captures.items():
            if tag.startswith("definition."):
                for node in nodes:
                    def_node_ids.add(node.id)

        tags = []
        scopes = []
        seen_scopes = set()
        saw = set()
        for node, tag in all_nodes:
            if tag.startswith("name.definition."):
                kind = "def"
            elif tag.startswith("name.reference."):
                kind = "ref"
            else:
                continue

            saw.add(kind)

            name = node.text.decode("utf-8")

            tags.append(
                Tag(
                    rel_fname=rel_fname,
                    fname=fname,
                    name=name,
                    kind=kind,
                    line=node.start_point[0],
                )
            )

            if kind != "def":
                continue

            start_line, end_line = self.get_definition_range(node, def_node_ids)
            scope = Scope(
                rel_fname=rel_fname,
                fname=fname,
                name=name,
                kind=tag[len("name.definition.") :],
                start_line=start_line,
                end_line=end_line,
            )
            if scope not in seen_scopes:
                seen_scopes.add(scope)
                scopes.append(scope)

        if "ref" in saw:
            return tags, scopes
        if "def" not in saw:
            return tags, scopes

        # We saw defs, without any refs
        # Some tags files only provide defs (cpp, for example)
        # Use pygments to backfill refs

        try:
            lexer = guess_lexer_for_filename(fname, code)
        except Exception:  # On Windows, bad ref to time.clock which is deprecated?
            # self.io.tool_error(f"Error lexing {fname}")
            return tags, scopes

        tokens = list(lexer.get_tokens(code))
        tokens = [token[1] for token in tokens if token[0] in Token.Name]

        for token in tokens:
            tags.append(
                Tag(
                    rel_fname=rel_fname,
                    fname=fname,
                    name=token,
                    kind="ref",
                    line=-1,
                )
            )

        return tags, scopes

    def get_symbol_index(self, fnames, progress=None):
        """Build (or reuse) a SymbolIndex over `fnames`.

        The index is rebuilt when the file set or any mtime changes, which is
        the same staleness rule the tags cache uses.
        """

        fnames = sorted(set(fnames))

        key = []
        for fname in fnames:
            try:
                key.append((fname, os.path.getmtime(fname)))
            except OSError:
                continue
        key = tuple(key)

        if self.symbol_index is not None and key == self.symbol_index_key:
            return self.symbol_index

        index = SymbolIndex()
        for fname, _mtime in key:
            if progress:
                progress(f"Indexing symbols: {fname}")

            rel_fname = self.get_rel_fname(fname)
            try:
                tags, scopes = self.get_tags_and_scopes(fname, rel_fname)
            except Exception as err:
                if self.verbose:
                    self.io.tool_warning(f"Unable to index {fname}: {err}")
                continue

            index.add_file(rel_fname, fname, tags, scopes)

        self.symbol_index = index
        self.symbol_index_key = key

        return index

    def get_ranked_tags(
        self, chat_fnames, other_fnames, mentioned_fnames, mentioned_idents, progress=None
    ):
        import networkx as nx

        defines = defaultdict(set)
        references = defaultdict(list)
        definitions = defaultdict(set)

        personalization = dict()

        fnames = set(chat_fnames).union(set(other_fnames))
        chat_rel_fnames = set()

        fnames = sorted(fnames)

        # Default personalization for unspecified files is 1/num_nodes
        # https://networkx.org/documentation/stable/_modules/networkx/algorithms/link_analysis/pagerank_alg.html#pagerank
        personalize = 100 / len(fnames)

        try:
            cache_size = len(self.TAGS_CACHE)
        except SQLITE_ERRORS as e:
            self.tags_cache_error(e)
            cache_size = len(self.TAGS_CACHE)

        if len(fnames) - cache_size > 100:
            self.io.tool_output(
                "Initial repo scan can be slow in larger repos, but only happens once."
            )
            fnames = tqdm(fnames, desc="Scanning repo")
            showing_bar = True
        else:
            showing_bar = False

        for fname in fnames:
            if self.verbose:
                self.io.tool_output(f"Processing {fname}")
            if progress and not showing_bar:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {fname}")

            try:
                file_ok = Path(fname).is_file()
            except OSError:
                file_ok = False

            if not file_ok:
                if fname not in self.warned_files:
                    self.io.tool_warning(f"Repo-map can't include {fname}")
                    self.io.tool_output(
                        "Has it been deleted from the file system but not from git?"
                    )
                    self.warned_files.add(fname)
                continue

            # dump(fname)
            rel_fname = self.get_rel_fname(fname)
            current_pers = 0.0  # Start with 0 personalization score

            if fname in chat_fnames:
                current_pers += personalize
                chat_rel_fnames.add(rel_fname)

            if rel_fname in mentioned_fnames:
                # Use max to avoid double counting if in chat_fnames and mentioned_fnames
                current_pers = max(current_pers, personalize)

            # Check path components against mentioned_idents
            path_obj = Path(rel_fname)
            path_components = set(path_obj.parts)
            basename_with_ext = path_obj.name
            basename_without_ext, _ = os.path.splitext(basename_with_ext)
            components_to_check = path_components.union({basename_with_ext, basename_without_ext})

            matched_idents = components_to_check.intersection(mentioned_idents)
            if matched_idents:
                # Add personalization *once* if any path component matches a mentioned ident
                current_pers += personalize

            if current_pers > 0:
                personalization[rel_fname] = current_pers  # Assign the final calculated value

            tags = list(self.get_tags(fname, rel_fname))
            if tags is None:
                continue

            for tag in tags:
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)
                    key = (rel_fname, tag.name)
                    definitions[key].add(tag)

                elif tag.kind == "ref":
                    references[tag.name].append(rel_fname)

        ##
        # dump(defines)
        # dump(references)
        # dump(personalization)

        if not references:
            references = dict((k, list(v)) for k, v in defines.items())

        idents = set(defines.keys()).intersection(set(references.keys()))

        G = nx.MultiDiGraph()

        # Add a small self-edge for every definition that has no references
        # Helps with tree-sitter 0.23.2 with ruby, where "def greet(name)"
        # isn't counted as a def AND a ref. tree-sitter 0.24.0 does.
        for ident in defines.keys():
            if ident in references:
                continue
            for definer in defines[ident]:
                G.add_edge(definer, definer, weight=0.1, ident=ident)

        for ident in idents:
            if progress:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {ident}")

            definers = defines[ident]

            mul = 1.0

            is_snake = ("_" in ident) and any(c.isalpha() for c in ident)
            is_kebab = ("-" in ident) and any(c.isalpha() for c in ident)
            is_camel = any(c.isupper() for c in ident) and any(c.islower() for c in ident)
            if ident in mentioned_idents:
                mul *= 10
            if (is_snake or is_kebab or is_camel) and len(ident) >= 8:
                mul *= 10
            if ident.startswith("_"):
                mul *= 0.1
            if len(defines[ident]) > 5:
                mul *= 0.1

            for referencer, num_refs in Counter(references[ident]).items():
                for definer in definers:
                    # dump(referencer, definer, num_refs, mul)
                    # if referencer == definer:
                    #    continue

                    use_mul = mul
                    if referencer in chat_rel_fnames:
                        use_mul *= 50

                    # scale down so high freq (low value) mentions don't dominate
                    num_refs = math.sqrt(num_refs)

                    G.add_edge(referencer, definer, weight=use_mul * num_refs, ident=ident)

        if not references:
            pass

        if personalization:
            pers_args = dict(personalization=personalization, dangling=personalization)
        else:
            pers_args = dict()

        try:
            ranked = nx.pagerank(G, weight="weight", **pers_args)
        except ZeroDivisionError:
            # Issue #1536
            try:
                ranked = nx.pagerank(G, weight="weight")
            except ZeroDivisionError:
                return []

        self.last_ranked = dict(ranked)

        # distribute the rank from each source node, across all of its out edges
        ranked_definitions = defaultdict(float)
        for src in G.nodes:
            if progress:
                progress(f"{UPDATING_REPO_MAP_MESSAGE}: {src}")

            src_rank = ranked[src]
            total_weight = sum(data["weight"] for _src, _dst, data in G.out_edges(src, data=True))
            # dump(src, src_rank, total_weight)
            for _src, dst, data in G.out_edges(src, data=True):
                data["rank"] = src_rank * data["weight"] / total_weight
                ident = data["ident"]
                ranked_definitions[(dst, ident)] += data["rank"]

        ranked_tags = []
        ranked_definitions = sorted(
            ranked_definitions.items(), reverse=True, key=lambda x: (x[1], x[0])
        )

        # dump(ranked_definitions)

        for (fname, ident), rank in ranked_definitions:
            # print(f"{rank:.03f} {fname} {ident}")
            if fname in chat_rel_fnames:
                continue
            ranked_tags += list(definitions.get((fname, ident), []))

        rel_other_fnames_without_tags = set(self.get_rel_fname(fname) for fname in other_fnames)

        fnames_already_included = set(rt[0] for rt in ranked_tags)

        top_rank = sorted([(rank, node) for (node, rank) in ranked.items()], reverse=True)
        for rank, fname in top_rank:
            if fname in rel_other_fnames_without_tags:
                rel_other_fnames_without_tags.remove(fname)
            if fname not in fnames_already_included:
                ranked_tags.append((fname,))

        for fname in rel_other_fnames_without_tags:
            ranked_tags.append((fname,))

        return ranked_tags

    def get_ranked_tags_map(
        self,
        chat_fnames,
        other_fnames=None,
        max_map_tokens=None,
        mentioned_fnames=None,
        mentioned_idents=None,
        force_refresh=False,
    ):
        # Create a cache key
        cache_key = [
            tuple(sorted(chat_fnames)) if chat_fnames else None,
            tuple(sorted(other_fnames)) if other_fnames else None,
            max_map_tokens,
        ]

        if self.refresh == "auto":
            cache_key += [
                tuple(sorted(mentioned_fnames)) if mentioned_fnames else None,
                tuple(sorted(mentioned_idents)) if mentioned_idents else None,
            ]
        cache_key = tuple(cache_key)

        use_cache = False
        if not force_refresh:
            if self.refresh == "manual" and self.last_map:
                return self.last_map

            if self.refresh == "always":
                use_cache = False
            elif self.refresh == "files":
                use_cache = True
            elif self.refresh == "auto":
                use_cache = self.map_processing_time > 1.0

            # Check if the result is in the cache
            if use_cache and cache_key in self.map_cache:
                return self.map_cache[cache_key]

        # If not in cache or force_refresh is True, generate the map
        start_time = time.time()
        result = self.get_ranked_tags_map_uncached(
            chat_fnames, other_fnames, max_map_tokens, mentioned_fnames, mentioned_idents
        )
        end_time = time.time()
        self.map_processing_time = end_time - start_time

        # Store the result in the cache
        self.map_cache[cache_key] = result
        self.last_map = result

        return result

    def get_ranked_tags_map_uncached(
        self,
        chat_fnames,
        other_fnames=None,
        max_map_tokens=None,
        mentioned_fnames=None,
        mentioned_idents=None,
    ):
        if not other_fnames:
            other_fnames = list()
        if not max_map_tokens:
            max_map_tokens = self.max_map_tokens
        if not mentioned_fnames:
            mentioned_fnames = set()
        if not mentioned_idents:
            mentioned_idents = set()

        spin = Spinner(UPDATING_REPO_MAP_MESSAGE)

        ranked_tags = self.get_ranked_tags(
            chat_fnames,
            other_fnames,
            mentioned_fnames,
            mentioned_idents,
            progress=spin.step,
        )

        other_rel_fnames = sorted(set(self.get_rel_fname(fname) for fname in other_fnames))
        special_fnames = filter_important_files(other_rel_fnames)
        ranked_tags_fnames = set(tag[0] for tag in ranked_tags)
        special_fnames = [fn for fn in special_fnames if fn not in ranked_tags_fnames]
        special_fnames = [(fn,) for fn in special_fnames]

        ranked_tags = special_fnames + ranked_tags

        spin.step()

        num_tags = len(ranked_tags)
        lower_bound = 0
        upper_bound = num_tags
        best_tree = None
        best_tree_tokens = 0

        chat_rel_fnames = set(self.get_rel_fname(fname) for fname in chat_fnames)

        self.tree_cache = dict()

        middle = min(int(max_map_tokens // 25), num_tags)
        while lower_bound <= upper_bound:
            # dump(lower_bound, middle, upper_bound)

            if middle > 1500:
                show_tokens = f"{middle / 1000.0:.1f}K"
            else:
                show_tokens = str(middle)
            spin.step(f"{UPDATING_REPO_MAP_MESSAGE}: {show_tokens} tokens")

            tree = self.to_tree(ranked_tags[:middle], chat_rel_fnames)
            num_tokens = self.token_count(tree)

            pct_err = abs(num_tokens - max_map_tokens) / max_map_tokens
            ok_err = 0.15
            if (num_tokens <= max_map_tokens and num_tokens > best_tree_tokens) or pct_err < ok_err:
                best_tree = tree
                best_tree_tokens = num_tokens

                if pct_err < ok_err:
                    break

            if num_tokens < max_map_tokens:
                lower_bound = middle + 1
            else:
                upper_bound = middle - 1

            middle = int((lower_bound + upper_bound) // 2)

        spin.end()
        return best_tree

    tree_cache = dict()

    def render_tree(self, abs_fname, rel_fname, lois):
        mtime = self.get_mtime(abs_fname)
        key = (rel_fname, tuple(sorted(lois)), mtime)

        if key in self.tree_cache:
            return self.tree_cache[key]

        if (
            rel_fname not in self.tree_context_cache
            or self.tree_context_cache[rel_fname]["mtime"] != mtime
        ):
            code = self.io.read_text(abs_fname) or ""
            if not code.endswith("\n"):
                code += "\n"

            context = TreeContext(
                rel_fname,
                code,
                color=False,
                line_number=False,
                child_context=False,
                last_line=False,
                margin=0,
                mark_lois=False,
                loi_pad=0,
                # header_max=30,
                show_top_of_file_parent_scope=False,
            )
            self.tree_context_cache[rel_fname] = {"context": context, "mtime": mtime}

        context = self.tree_context_cache[rel_fname]["context"]
        context.lines_of_interest = set()
        context.add_lines_of_interest(lois)
        context.add_context()
        res = context.format()
        self.tree_cache[key] = res
        return res

    def to_tree(self, tags, chat_rel_fnames):
        if not tags:
            return ""

        cur_fname = None
        cur_abs_fname = None
        lois = None
        output = ""

        # add a bogus tag at the end so we trip the this_fname != cur_fname...
        dummy_tag = (None,)
        for tag in sorted(tags) + [dummy_tag]:
            this_rel_fname = tag[0]
            if this_rel_fname in chat_rel_fnames:
                continue

            # ... here ... to output the final real entry in the list
            if this_rel_fname != cur_fname:
                if lois is not None:
                    output += "\n"
                    output += cur_fname + ":\n"
                    output += self.render_tree(cur_abs_fname, cur_fname, lois)
                    lois = None
                elif cur_fname:
                    output += "\n" + cur_fname + "\n"
                if type(tag) is Tag:
                    lois = []
                    cur_abs_fname = tag.fname
                cur_fname = this_rel_fname

            if lois is not None:
                lois.append(tag.line)

        # truncate long lines, in case we get minified js or something else crazy
        output = "\n".join([line[:100] for line in output.splitlines()]) + "\n"

        return output


class SymbolIndex:
    """Definitions, references and definition ranges for a set of files.

    Built from the same tree-sitter tags that drive the repo map, so it is
    cheap to (re)build once the tags cache is warm.
    """

    def __init__(self):
        self.defs = defaultdict(list)  # name -> [Tag]
        self.refs = defaultdict(list)  # name -> [Tag]
        self.refs_by_file = defaultdict(list)  # rel_fname -> [Tag], sorted by line
        self.scopes = dict()  # rel_fname -> [Scope], sorted outermost first
        self.abs_fnames = dict()  # rel_fname -> abs fname

    def add_file(self, rel_fname, abs_fname, tags, scopes):
        self.abs_fnames[rel_fname] = abs_fname

        file_refs = []
        # Some tags queries capture the same name on the same line more than once
        seen = set()
        for tag in tags:
            key = (tag.kind, tag.name, tag.line)
            if key in seen:
                continue
            seen.add(key)

            if tag.kind == "def":
                self.defs[tag.name].append(tag)
            elif tag.kind == "ref" and tag.line >= 0:
                # line == -1 marks a pygments backfill, which has no usable location
                self.refs[tag.name].append(tag)
                file_refs.append(tag)

        if file_refs:
            self.refs_by_file[rel_fname] = sorted(file_refs, key=lambda tag: tag.line)

        if scopes:
            self.scopes[rel_fname] = sorted(scopes, key=lambda s: (s.start_line, -s.end_line))

    def refs_in_range(self, rel_fname, start_line, end_line):
        return [
            tag
            for tag in self.refs_by_file.get(rel_fname, [])
            if start_line <= tag.line <= end_line
        ]

    def enclosing_scopes(self, rel_fname, line):
        """Definitions containing `line`, outermost first."""
        return [
            scope
            for scope in self.scopes.get(rel_fname, [])
            if scope.start_line <= line <= scope.end_line
        ]

    def innermost_scope(self, rel_fname, line):
        scopes = self.enclosing_scopes(rel_fname, line)
        if scopes:
            return scopes[-1]

    def scope_for_def(self, tag):
        """The Scope that a def Tag names."""
        for scope in self.scopes.get(tag.rel_fname, []):
            if scope.start_line == tag.line and scope.name == tag.name:
                return scope

        # Some languages capture the name on a different line than the definition
        for scope in self.enclosing_scopes(tag.rel_fname, tag.line):
            if scope.name == tag.name:
                return scope

    def qualified_name(self, scope):
        """`Class.method` style name, built from enclosing definitions."""
        parents = [
            outer.name
            for outer in self.enclosing_scopes(scope.rel_fname, scope.start_line)
            if outer != scope and outer.end_line >= scope.end_line
        ]
        return ".".join(parents + [scope.name])

    def def_names(self):
        return sorted(self.defs.keys())


def find_src_files(directory):
    if not os.path.isdir(directory):
        return [directory]

    src_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            src_files.append(os.path.join(root, file))
    return src_files


def get_random_color():
    hue = random.random()
    r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(hue, 1, 0.75)]
    res = f"#{r:02x}{g:02x}{b:02x}"
    return res


def get_scm_fname(lang):
    # Load the tags queries
    if USING_TSL_PACK:
        subdir = "tree-sitter-language-pack"
        try:
            path = resources.files(__package__).joinpath(
                "queries",
                subdir,
                f"{lang}-tags.scm",
            )
            if path.exists():
                return path
        except KeyError:
            pass

    # Fall back to tree-sitter-languages
    subdir = "tree-sitter-languages"
    try:
        return resources.files(__package__).joinpath(
            "queries",
            subdir,
            f"{lang}-tags.scm",
        )
    except KeyError:
        return


def get_supported_languages_md():
    from grep_ast.parsers import PARSERS

    res = """
| Language | File extension | Repo map | Linter |
|:--------:|:--------------:|:--------:|:------:|
"""
    data = sorted((lang, ex) for ex, lang in PARSERS.items())

    for lang, ext in data:
        fn = get_scm_fname(lang)
        repo_map = "✓" if Path(fn).exists() else ""
        linter_support = "✓"
        res += f"| {lang:20} | {ext:20} | {repo_map:^8} | {linter_support:^6} |\n"

    res += "\n"

    return res


if __name__ == "__main__":
    fnames = sys.argv[1:]

    chat_fnames = []
    other_fnames = []
    for fname in sys.argv[1:]:
        if Path(fname).is_dir():
            chat_fnames += find_src_files(fname)
        else:
            chat_fnames.append(fname)

    rm = RepoMap(root=".")
    repo_map = rm.get_ranked_tags_map(chat_fnames, other_fnames)

    dump(len(repo_map))
    print(repo_map)

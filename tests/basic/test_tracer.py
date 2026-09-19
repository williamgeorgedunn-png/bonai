import os
import unittest

from aider.dump import dump  # noqa: F401
from aider.io import InputOutput
from aider.models import Model
from aider.repomap import RepoMap
from aider.tracer import (
    RepoTracer,
    TraceRequest,
    clean_symbol,
    parse_trace_line,
    parse_trace_requests,
)
from aider.utils import IgnorantTemporaryDirectory

SERVICE_PY = """\
from storage import save_record


def normalize(raw_value):
    cleaned = raw_value.strip()
    return cleaned


def handle_request(raw_value):
    payload = normalize(raw_value)
    save_record(payload)
    return payload
"""

STORAGE_PY = """\
RETRY_LIMIT = 3


def save_record(payload):
    attempts = RETRY_LIMIT
    while attempts:
        attempts = attempts - 1
    return payload
"""

API_PY = """\
from service import handle_request


def post(raw_value):
    return handle_request(raw_value)


def put(raw_value):
    return handle_request(raw_value)
"""

UNRELATED_PY = """\
def lonely_helper():
    return 42
"""

CLI_PY = """\
from service import handle_request


def main(raw_value):
    return handle_request(raw_value)
"""

WORKER_PY = """\
from service import handle_request


def consume(raw_value):
    return handle_request(raw_value)
"""


class TestTraceParsing(unittest.TestCase):
    def test_clean_symbol_strips_decoration(self):
        self.assertEqual(clean_symbol("`foo`"), "foo")
        self.assertEqual(clean_symbol("foo()"), "foo")
        self.assertEqual(clean_symbol("foo(a, b)"), "foo")
        self.assertEqual(clean_symbol("**foo**,"), "foo")
        self.assertEqual(clean_symbol("self.foo"), "foo")
        self.assertEqual(clean_symbol("this.foo"), "foo")
        self.assertEqual(clean_symbol("Class.method"), "Class.method")
        self.assertEqual(clean_symbol("'foo'."), "foo")
        self.assertEqual(clean_symbol("some words here"), "")
        self.assertEqual(clean_symbol(""), "")

    def test_parse_trace_line_directions(self):
        self.assertEqual(parse_trace_line("foo").direction, "both")
        self.assertEqual(parse_trace_line("foo up").direction, "up")
        self.assertEqual(parse_trace_line("foo callers").direction, "up")
        self.assertEqual(parse_trace_line("foo down").direction, "down")
        self.assertEqual(parse_trace_line("foo callees").direction, "down")
        self.assertEqual(parse_trace_line("- `foo()` upstream").symbol, "foo")
        self.assertEqual(parse_trace_line("1. foo").symbol, "foo")

    def test_parse_trace_line_depth_and_file(self):
        req = parse_trace_line("foo depth 2")
        self.assertEqual(req.depth, 2)

        req = parse_trace_line("foo depth=2")
        self.assertEqual(req.depth, 2)

        # depth is capped
        self.assertEqual(parse_trace_line("foo depth 9").depth, 2)

        req = parse_trace_line("foo in aider/main.py")
        self.assertEqual(req.file, "aider/main.py")
        self.assertEqual(req.symbol, "foo")

        req = parse_trace_line("aider/main.py:foo")
        self.assertEqual(req.file, "aider/main.py")
        self.assertEqual(req.symbol, "foo")

    def test_parse_fenced_trace_block(self):
        content = """\
I need to see how this is wired up first.

```trace
handle_request
save_record up
```

Then I can make the change.
"""
        reqs = parse_trace_requests(content)
        self.assertEqual([req.symbol for req in reqs], ["handle_request", "save_record"])
        self.assertEqual([req.direction for req in reqs], ["both", "up"])
        self.assertTrue(all(req.strict for req in reqs))

    def test_parse_fence_direction_applies_to_the_block(self):
        content = "```trace up\nhandle_request\nsave_record down\n```\n"
        reqs = parse_trace_requests(content)

        self.assertEqual(reqs[0].direction, "up")
        # An explicit direction on the line still wins
        self.assertEqual(reqs[1].direction, "down")

    def test_parse_ignores_other_fenced_blocks(self):
        content = """\
Here is the edit:

```python
def trace_something():
    trace: not_a_request
```

aider/main.py
```
<<<<<<< SEARCH
trace foo
=======
trace bar
>>>>>>> REPLACE
```
"""
        self.assertEqual(parse_trace_requests(content), [])

    def test_parse_loose_requests_are_not_strict(self):
        content = "Please trace handle_request for me.\n"
        reqs = parse_trace_requests(content)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].symbol, "handle_request")
        self.assertFalse(reqs[0].strict)

    def test_parse_caps_number_of_requests(self):
        content = "```trace\na\nb\nc\nd\ne\n```\n"
        self.assertEqual(len(parse_trace_requests(content)), 3)

    def test_parse_dedupes_requests(self):
        content = "```trace\nfoo\n`foo`\nfoo()\n```\n"
        self.assertEqual(len(parse_trace_requests(content)), 1)

    def test_parse_empty_content(self):
        self.assertEqual(parse_trace_requests(""), [])
        self.assertEqual(parse_trace_requests(None), [])


class TestRepoTracer(unittest.TestCase):
    def setUp(self):
        self.GPT35 = Model("gpt-3.5-turbo")
        self.temp_dir_obj = IgnorantTemporaryDirectory()
        self.temp_dir = self.temp_dir_obj.name

        self.files = {
            "service.py": SERVICE_PY,
            "storage.py": STORAGE_PY,
            "api.py": API_PY,
            "cli.py": CLI_PY,
            "worker.py": WORKER_PY,
            "unrelated.py": UNRELATED_PY,
        }
        for fname, content in self.files.items():
            with open(os.path.join(self.temp_dir, fname), "w") as f:
                f.write(content)

        self.io = InputOutput()
        self.repo_map = RepoMap(main_model=self.GPT35, root=self.temp_dir, io=self.io)
        self.tracer = RepoTracer(self.repo_map, self.io, self.temp_dir, max_tokens=2048)
        self.abs_fnames = [os.path.join(self.temp_dir, fname) for fname in self.files]

    def tearDown(self):
        del self.tracer
        del self.repo_map
        self.temp_dir_obj.cleanup()

    def trace(self, symbol, direction="both", file=None, chat_rel_fnames=()):
        req = TraceRequest(
            symbol=symbol, direction=direction, depth=1, file=file, strict=True
        )
        return self.tracer.trace(req, self.abs_fnames, chat_rel_fnames=chat_rel_fnames)

    def test_index_finds_definitions_and_scopes(self):
        index = self.repo_map.get_symbol_index(self.abs_fnames)

        self.assertIn("handle_request", index.defs)
        self.assertIn("normalize", index.defs)

        tag = index.defs["handle_request"][0]
        self.assertEqual(tag.rel_fname, "service.py")

        scope = index.scope_for_def(tag)
        self.assertIsNotNone(scope)
        self.assertEqual(scope.kind, "function")
        # The scope must span the whole function body, not just its name
        self.assertGreater(scope.end_line, scope.start_line)
        self.assertEqual(index.qualified_name(scope), "handle_request")

    def test_index_is_reused_until_a_file_changes(self):
        first = self.repo_map.get_symbol_index(self.abs_fnames)
        self.assertIs(first, self.repo_map.get_symbol_index(self.abs_fnames))

        path = os.path.join(self.temp_dir, "service.py")
        os.utime(path, (0, 0))

        self.assertIsNot(first, self.repo_map.get_symbol_index(self.abs_fnames))

    def test_trace_function_finds_callers(self):
        result = self.trace("handle_request", direction="up")

        self.assertIn("handle_request", result)
        self.assertIn("api.py", result)
        self.assertIn("def post", result)
        self.assertIn("def put", result)
        # Its own definition file is not a caller
        self.assertNotIn("unrelated.py", result)

    def test_trace_function_finds_callees(self):
        result = self.trace("handle_request", direction="down")

        self.assertIn("normalize", result)
        self.assertIn("save_record", result)
        self.assertIn("storage.py", result)
        # Callees are listed as locations, not pasted in full
        self.assertNotIn("def save_record(payload):", result)

    def test_trace_excludes_files_already_in_chat(self):
        result = self.trace("handle_request", direction="up", chat_rel_fnames={"api.py"})

        self.assertIn("already in the chat", result)
        self.assertNotIn("def post", result)

    def test_trace_reports_definition_location(self):
        result = self.trace("normalize")
        self.assertIn("service.py:4", result)

    def test_trace_unknown_symbol_suggests_similar_names(self):
        result = self.trace("handle_requst")

        self.assertIn("No uses", result)
        self.assertIn("handle_request", result)

    def test_trace_unknown_symbol_without_matches(self):
        result = self.trace("zzzz_not_here")

        self.assertIn("No uses", result)
        self.assertNotIn("Similar names", result)

    def test_trace_variable_separates_writes_and_reads(self):
        result = self.trace("attempts")

        self.assertIn("is set", result)
        self.assertIn("is read", result)
        self.assertIn("storage.py", result)

    def test_trace_variable_reports_value_flow(self):
        result = self.trace("payload")

        self.assertIn("How the value flows", result)
        self.assertIn("normalize()", result)

    def test_trace_variable_scoped_to_one_file(self):
        result = self.trace("payload", file="storage.py")

        self.assertIn("storage.py", result)
        self.assertNotIn("service.py:", result)

    def test_trace_constant_read(self):
        result = self.trace("RETRY_LIMIT")
        self.assertIn("storage.py", result)

    def test_trace_is_deterministic(self):
        self.assertEqual(self.trace("handle_request"), self.trace("handle_request"))

    def test_trace_respects_token_budget(self):
        small = self.tracer.trace(
            TraceRequest("handle_request", "both", 1, None, True),
            self.abs_fnames,
            max_tokens=220,
        )
        large = self.tracer.trace(
            TraceRequest("handle_request", "both", 1, None, True),
            self.abs_fnames,
            max_tokens=4096,
        )

        self.assertLess(len(small), len(large))
        # Even a tiny budget still names the symbol and where to look
        self.assertIn("handle_request", small)
        self.assertIn("Also used in:", small)

    def test_trace_with_a_tiny_budget_still_says_where_the_symbol_is(self):
        result = self.tracer.trace(
            TraceRequest("handle_request", "both", 1, None, True),
            self.abs_fnames,
            max_tokens=60,
        )

        self.assertIn("handle_request", result)
        self.assertIn("service.py", result)

    def test_trace_never_exceeds_its_budget(self):
        for symbol in ("handle_request", "payload", "normalize"):
            for budget in (120, 400, 2048):
                result = self.tracer.trace(
                    TraceRequest(symbol, "both", 1, None, True),
                    self.abs_fnames,
                    max_tokens=budget,
                )
                self.assertLessEqual(
                    self.repo_map.token_count(result),
                    budget,
                    f"{symbol} at budget {budget}",
                )

    def test_trace_always_tells_the_model_what_to_do_next(self):
        result = self.trace("handle_request")
        self.assertIn("add", result.lower())

    def test_trace_output_has_line_numbers(self):
        result = self.trace("handle_request", direction="up")
        self.assertRegex(result, r"\n\s*\d+│")


if __name__ == "__main__":
    unittest.main()

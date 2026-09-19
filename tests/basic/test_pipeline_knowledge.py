import unittest
from pathlib import Path

from aider.io import InputOutput
from aider.models import Model
from aider.pipeline.briefs import clean_brief, extract_snippet_refs, resolve_snippets
from aider.pipeline.config import PipelineConfig
from aider.pipeline.knowledge import KnowledgeService
from aider.pipeline.parsing import Need
from aider.repomap import RepoMap
from aider.tracer import RepoTracer
from aider.utils import GitTemporaryDirectory

CLIENT = '''\
import time


class ApiClient:
    """Talks to the API."""

    def __init__(self, base_url, session=None):
        self.base_url = base_url
        self.session = session

    def request(self, method, path, **kw):
        started = time.time()
        return self.send(method, path, started, **kw)

    def send(self, method, path, started, **kw):
        return {"method": method, "path": path, "started": started}
'''

CALLER = """\
from client import ApiClient


def fetch_users(base_url):
    client = ApiClient(base_url)
    return client.request("GET", "/users")
"""


class KnowledgeTestCase(unittest.TestCase):
    def build(self, root, digest_fn=None, config=None):
        io = InputOutput(yes=True)
        model = Model("gpt-4o")
        repo_map = RepoMap(map_tokens=1024, root=root, main_model=model, io=io)
        tracer = RepoTracer(repo_map, io, root, max_tokens=1024)
        files = [str(p) for p in Path(root).rglob("*.py")]
        return KnowledgeService(
            root,
            io,
            config or PipelineConfig(),
            repo_map=repo_map,
            tracer=tracer,
            token_count=model.token_count,
            digest_fn=digest_fn,
            get_all_abs_files=lambda: files,
            cache_dir=Path(root) / ".aider.pipeline" / "digests",
        )

    def write_repo(self, root):
        (Path(root) / "client.py").write_text(CLIENT)
        (Path(root) / "caller.py").write_text(CALLER)


class TestLookups(KnowledgeTestCase):
    def test_outline_lists_definitions_with_lines(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            fact = self.build(root).answer(Need("outline", target="client.py"))
            self.assertIn("ApiClient", fact.text)
            self.assertIn("request", fact.text)
            self.assertRegex(fact.text, r"L\d+-L\d+")
            # An outline is signatures, not bodies
            self.assertNotIn("started = time.time()", fact.text)

    def test_outline_updates_after_an_edit_without_adding_files(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            knowledge = self.build(root)
            before = knowledge.answer(Need("outline", target="client.py"))
            before_span = None
            for line in before.text.splitlines():
                if ".request" in line or line.startswith("- request"):
                    before_span = line
                    break
            self.assertIsNotNone(before_span)

            source = Path(root) / "client.py"
            extra = "\n".join(f"        x{n} = {n}" for n in range(30))
            source.write_text(CLIENT.replace("started = time.time()", extra, 1))
            source.touch()

            after = knowledge.answer(Need("outline", target="client.py"))
            after_span = None
            for line in after.text.splitlines():
                if ".request" in line or line.startswith("- request"):
                    after_span = line
                    break
            self.assertIsNotNone(after_span)
            self.assertNotEqual(
                before_span,
                after_span,
                "the outline should pick up the new end line after an in-place edit",
            )

    def test_outline_matches_a_partial_path(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "net").mkdir()
            (Path(root) / "net" / "client.py").write_text(CLIENT)
            fact = self.build(root).answer(Need("outline", target="client.py"))
            self.assertIn("ApiClient", fact.text)

    def test_refs_reports_files_and_lines(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            fact = self.build(root).answer(Need("refs", symbol="request"))
            self.assertIn("caller.py", fact.text)
            self.assertIn("Defined at", fact.text)

    def test_refs_falls_back_to_search(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            (Path(root) / "notes.txt").write_text("mentions MAGIC_TOKEN here\n")
            knowledge = self.build(root)
            knowledge.get_all_abs_files = lambda: [str(Path(root) / "notes.txt")]
            fact = knowledge.answer(Need("refs", symbol="MAGIC_TOKEN"))
            self.assertIn("notes.txt:1", fact.text)

    def test_grep_caps_hits(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "many.py").write_text("\n".join("hit = 1" for _ in range(200)))
            knowledge = self.build(root, config=PipelineConfig(grep_hits=5))
            fact = knowledge.answer(Need("grep", target="hit"))
            self.assertEqual(fact.text.count("many.py:"), 5)
            self.assertIn("stopped at 5", fact.text)

    def test_grep_reports_a_bad_pattern(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            fact = self.build(root).answer(Need("grep", target="[unclosed"))
            self.assertIn("Invalid pattern", fact.text)

    def test_source_returns_one_symbol_only(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            fact = self.build(root).answer(Need("source", target="client.py", symbol="request"))
            self.assertIn("started = time.time()", fact.text)
            self.assertNotIn("self.base_url = base_url", fact.text)

    def test_source_returns_a_line_range(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            need = Need("source", target="client.py", start_line=1, end_line=2)
            fact = self.build(root).answer(need)
            self.assertIn("import time", fact.text)
            self.assertNotIn("class ApiClient", fact.text)

    def test_symbol_range_finds_qualified_and_bare_names(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            knowledge = self.build(root)
            self.assertIsNotNone(knowledge.symbol_range("client.py", "ApiClient.request"))
            self.assertIsNotNone(knowledge.symbol_range("client.py", "request"))
            self.assertIsNone(knowledge.symbol_range("client.py", "nope"))

    def test_unknown_need_kind_is_ignored(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            self.assertIsNone(self.build(root).answer(Need("telepathy", target="client.py")))

    def test_a_failing_lookup_does_not_raise(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            knowledge = self.build(root)
            knowledge.index = lambda: (_ for _ in ()).throw(RuntimeError("index exploded"))
            facts, _omitted = knowledge.resolve([Need("outline", target="client.py")])
            self.assertIn("Lookup failed", facts[0].text)


class TestDigestCache(KnowledgeTestCase):
    def test_digest_is_written_once_and_reused(self):
        calls = []

        def digest_fn(text, label):
            calls.append(label)
            return "Purpose: sends a request"

        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            knowledge = self.build(root, digest_fn=digest_fn)
            need = Need("digest", target="client.py", symbol="request")

            first = knowledge.answer(need)
            self.assertIn("sends a request", first.text)
            self.assertEqual(len(calls), 1)

            second = knowledge.answer(need)
            self.assertIn("sends a request", second.text)
            self.assertEqual(len(calls), 1, "a cached digest should cost no worker call")
            self.assertEqual(knowledge.digest_hits, 1)

    def test_editing_a_symbol_invalidates_only_its_digest(self):
        calls = []

        def digest_fn(text, label):
            calls.append(label)
            return f"Purpose: {len(calls)}"

        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            knowledge = self.build(root, digest_fn=digest_fn)
            request_need = Need("digest", target="client.py", symbol="request")
            send_need = Need("digest", target="client.py", symbol="send")

            knowledge.answer(request_need)
            knowledge.answer(send_need)
            self.assertEqual(len(calls), 2)

            source = Path(root) / "client.py"
            source.write_text(CLIENT.replace("started = time.time()", "started = 0.0"))
            source.touch()

            knowledge.answer(send_need)
            self.assertEqual(len(calls), 2, "the untouched symbol stays cached")
            knowledge.answer(request_need)
            self.assertEqual(len(calls), 3, "the edited symbol is digested again")

    def test_digest_without_a_worker_returns_a_message(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            fact = self.build(root).answer(Need("digest", target="client.py", symbol="request"))
            self.assertIn("unavailable", fact.text)


class TestFactBudget(KnowledgeTestCase):
    def test_facts_stop_at_the_budget(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "big.py").write_text(
                "\n".join(f"def function_{n}(a, b):\n    return a + b + {n}\n" for n in range(400))
            )
            knowledge = self.build(root)
            needs = [Need("outline", target="big.py") for _ in range(4)]
            facts, omitted = knowledge.resolve(needs, budget=200)
            total = sum(f.tokens for f in facts)
            self.assertLessEqual(total, 260)
            self.assertTrue(omitted or any("truncated" in f.text for f in facts))

    def test_zero_budget_omits_everything(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            facts, omitted = self.build(root).resolve([Need("outline", target="client.py")], 0)
            self.assertEqual(facts, [])
            self.assertEqual(omitted, 1)


class TestBriefs(KnowledgeTestCase):
    def test_snippet_refs_are_found(self):
        brief = "## Read-only context\nsnippet: client.py::request\n- snippet: caller.py:L1-L3\n"
        refs = [ref for _line, ref in extract_snippet_refs(brief)]
        self.assertEqual(refs, ["client.py::request", "caller.py:L1-L3"])

    def test_snippets_are_inlined_for_the_worker(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            knowledge = self.build(root)
            brief = "## Read-only context\nsnippet: client.py::request\n"
            text, resolved = resolve_snippets(brief, knowledge, 2000)
            self.assertIn("started = time.time()", text)
            self.assertNotIn("snippet: client.py::request", text)
            self.assertEqual(len(resolved), 1)

    def test_snippets_respect_the_cap(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "big.py").write_text(
                "def wide(a):\n" + "\n".join(f"    x{n} = {n}" for n in range(500)) + "\n"
            )
            knowledge = self.build(root)
            brief = "snippet: big.py::wide\n"
            text, _resolved = resolve_snippets(brief, knowledge, 50)
            self.assertLess(len(text.splitlines()), 200)

    def test_unresolvable_snippet_is_reported_not_dropped_silently(self):
        with GitTemporaryDirectory() as root:
            self.write_repo(root)
            brief = "snippet: ghost.py::missing\n"
            text, resolved = resolve_snippets(brief, self.build(root), 2000)
            self.assertIn("could not resolve snippet", text)
            self.assertEqual(resolved, [])

    def test_clean_brief_removes_protocol_lines(self):
        brief = clean_brief(
            "<think>maybe</think>\n"
            "# Task T1\n"
            "NEED: outline a.py\n"
            "VERDICT: RETRY\n"
            "## Changes\n"
            "- do the thing\n"
            "REMEMBER: a fact\n"
        )
        self.assertIn("## Changes", brief)
        self.assertIn("- do the thing", brief)
        self.assertNotIn("NEED:", brief)
        self.assertNotIn("VERDICT:", brief)
        self.assertNotIn("REMEMBER:", brief)
        self.assertNotIn("maybe", brief)


if __name__ == "__main__":
    unittest.main()

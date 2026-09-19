import unittest
from pathlib import Path

from aider.pipeline import parsing
from aider.pipeline.config import PipelineConfig
from aider.pipeline.ledger import Ledger, LedgerError
from aider.pipeline.memory import WorkingMemory
from aider.pipeline.verify import clamp, trim_test_output
from aider.utils import GitTemporaryDirectory


def count(text):
    return max(1, len(text.split()))


class TestPipelineConfig(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(PipelineConfig().validate(), [])

    def test_bad_approve_is_reported(self):
        problems = PipelineConfig(approve="sometimes").validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("approve", problems[0])

    def test_negative_budget_is_reported(self):
        problems = PipelineConfig(max_tasks=-1).validate()
        self.assertTrue(any("max_tasks" in p for p in problems))

    def test_budget_fields_generate_flags(self):
        fields = PipelineConfig.budget_fields()
        self.assertIn("working_memory_tokens", fields)
        self.assertIn("max_attempts", fields)
        self.assertNotIn("approve", fields)
        self.assertNotIn("tdd", fields)

    def test_from_args_ignores_unset(self):
        class Args:
            pipeline_max_tasks = 3
            pipeline_approve = None

        config = PipelineConfig.from_args(Args())
        self.assertEqual(config.max_tasks, 3)
        self.assertEqual(config.approve, "plan")


class TestParsing(unittest.TestCase):
    def test_strip_reasoning(self):
        text = "<think>hmm, maybe</think>\nVERDICT: ACCEPT"
        self.assertEqual(parsing.strip_reasoning(text), "VERDICT: ACCEPT")

    def test_strip_unterminated_reasoning(self):
        self.assertEqual(parsing.strip_reasoning("keep\n<think>dropped"), "keep")

    def test_directives(self):
        reply = "\n".join(
            [
                "Looks fine.",
                "VERDICT: ACCEPT",
                "NOTE: RateLimiter.acquire returns bool",
                "REMEMBER: tests live in tests/",
                "REMEMBER PINNED: ApiClient.request is the only HTTP entry point",
                "FORGET M4, M5",
                "QUESTION: should this be thread safe?",
                "NEED: outline net/client.py",
            ]
        )
        found = parsing.parse_directives(reply)
        self.assertEqual(found.verdict, "ACCEPT")
        self.assertEqual(found.notes, ["RateLimiter.acquire returns bool"])
        self.assertEqual(found.remember[0], ("tests live in tests/", False))
        self.assertEqual(found.remember[1][1], True)
        self.assertEqual(found.forget, ["M4", "M5"])
        self.assertEqual(len(found.questions), 1)
        self.assertEqual(found.needs[0].kind, "outline")
        self.assertEqual(found.needs[0].target, "net/client.py")

    def test_directives_inside_fences_are_left_alone(self):
        reply = "\n".join(
            [
                "Here is the brief.",
                "```python",
                "# NEED: outline not_a_request.py",
                "```",
            ]
        )
        found = parsing.parse_directives(reply)
        self.assertEqual(found.needs, [])
        self.assertIn("NEED: outline not_a_request.py", found.body)

    def test_need_forms(self):
        cases = {
            "outline a/b.py": ("outline", "a/b.py", "", None),
            "refs Foo.bar": ("refs", "", "Foo.bar", None),
            "digest a/b.py::Foo": ("digest", "a/b.py", "Foo", None),
            "source a/b.py:L10-L20": ("source", "a/b.py", "", 10),
            "grep def foo": ("grep", "def foo", "", None),
            "about Foo": ("about", "", "Foo", None),
        }
        for text, (kind, target, symbol, start) in cases.items():
            need = parsing.parse_need(text)
            self.assertIsNotNone(need, text)
            self.assertEqual((need.kind, need.target, need.symbol), (kind, target, symbol), text)
            self.assertEqual(need.start_line, start, text)

    def test_need_rejects_unknown_kind(self):
        self.assertIsNone(parsing.parse_need("summarise everything"))

    def test_windows_paths_are_normalised(self):
        need = parsing.parse_need(r"outline net\client.py")
        self.assertEqual(need.target, "net/client.py")

    def test_extract_yaml_prefers_labelled_block(self):
        reply = "prose\n```\nnot: yaml-we-want\n```\n```yaml\ntasks:\n  - id: T1\n```\n"
        data = parsing.extract_yaml(reply)
        self.assertEqual(data["tasks"][0]["id"], "T1")

    def test_extract_yaml_without_fence(self):
        self.assertEqual(parsing.extract_yaml("tasks:\n  - id: T9\n")["tasks"][0]["id"], "T9")

    def test_extract_yaml_raises_when_unparseable(self):
        with self.assertRaises(ValueError):
            parsing.extract_yaml("I suggest: first this: then that")

    def test_extract_yaml_from_prose_has_no_tasks(self):
        # Prose sometimes parses as a mapping; the caller must still see no tasks.
        data = parsing.extract_yaml("I would suggest that you: do the thing")
        self.assertIsNone(data.get("tasks") if isinstance(data, dict) else None)

    def test_find_verdict_fallback(self):
        found = parsing.find_verdict("I think we retry this", ("ACCEPT", "RETRY"))
        self.assertEqual(found, "RETRY")

    def test_parse_compact(self):
        keep, drop, rewrites = parsing.parse_compact(
            "KEEP M1\nDROP M2\nREWRITE M3: shorter fact\nnoise"
        )
        self.assertEqual(drop, ["M2"])
        self.assertIn("M1", keep)
        self.assertEqual(rewrites["M3"], "shorter fact")


class TestWorkingMemory(unittest.TestCase):
    def make(self, max_tokens=10):
        return WorkingMemory(max_tokens=max_tokens, token_count=count)

    def test_add_and_dedupe(self):
        memory = self.make()
        first = memory.add("one fact")
        again = memory.add("one fact")
        self.assertIs(first, again)
        self.assertEqual(len(memory.items), 1)

    def test_forget(self):
        memory = self.make()
        item = memory.add("drop me")
        self.assertTrue(memory.forget(item.id))
        self.assertEqual(memory.items, [])
        self.assertFalse(memory.forget("nope"))

    def test_eviction_is_oldest_first_and_spares_pinned(self):
        memory = self.make(max_tokens=6)
        memory.add("pinned fact here", pinned=True)
        memory.add("fact two here")
        memory.add("fact three here")
        dropped = memory.evict_to_fit()
        self.assertFalse(memory.over_budget())
        self.assertIn("pinned fact here", [i.text for i in memory.items])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(memory.evicted, 1)

    def test_pinned_overflow_is_detected(self):
        memory = self.make(max_tokens=2)
        memory.add("a much longer pinned fact", pinned=True)
        self.assertTrue(memory.pinned_overflow())
        memory.evict_to_fit()
        self.assertEqual(len(memory.items), 1)

    def test_roundtrip(self):
        memory = self.make()
        memory.add("keep this", pinned=True, source="static")
        other = self.make()
        other.load(memory.dump())
        self.assertEqual(other.items[0].text, "keep this")
        self.assertTrue(other.items[0].pinned)
        new_item = other.add("another")
        self.assertNotEqual(new_item.id, other.items[0].id)

    def test_render_shows_budget(self):
        memory = self.make()
        memory.add("some fact")
        rendered = memory.render()
        self.assertIn("some fact", rendered)
        self.assertIn("of 10 tokens", rendered)


class TestLedger(unittest.TestCase):
    def make(self, tasks=None, request="do a thing"):
        with GitTemporaryDirectory() as root:
            ledger = Ledger(Path(root) / ".aider.pipeline" / "ledger.yml", request=request)
            if tasks:
                ledger.set_tasks(tasks)
            return ledger

    def test_set_tasks_and_defaults(self):
        ledger = self.make([{"title": "Do it", "file": "a.py"}])
        task = ledger.tasks[0]
        self.assertEqual(task.id, "T1")
        self.assertEqual(task.kind, "edit")
        self.assertEqual(task.status, "pending")

    def test_missing_file_is_rejected(self):
        with self.assertRaises(LedgerError):
            self.make([{"title": "no file"}])

    def test_max_tasks_is_enforced(self):
        with self.assertRaises(LedgerError) as err:
            self.make(
                [{"title": f"t{i}", "file": f"{i}.py"} for i in range(4)],
            ).set_tasks([], max_tasks=3)
        self.assertIn("file", str(err.exception).lower() + "file")

    def test_unknown_dependency_is_rejected(self):
        with self.assertRaises(LedgerError):
            self.make([{"id": "T1", "title": "t", "file": "a.py", "depends_on": ["T9"]}])

    def test_cycles_are_rejected(self):
        with self.assertRaises(LedgerError) as err:
            self.make(
                [
                    {"id": "T1", "title": "t1", "file": "a.py", "depends_on": ["T2"]},
                    {"id": "T2", "title": "t2", "file": "b.py", "depends_on": ["T1"]},
                ]
            )
        self.assertIn("cycle", str(err.exception))

    def test_next_task_respects_dependencies(self):
        ledger = self.make(
            [
                {"id": "T1", "title": "iface", "file": "a.py"},
                {"id": "T2", "title": "caller", "file": "b.py", "depends_on": ["T1"]},
            ]
        )
        self.assertEqual(ledger.next_task().id, "T1")
        ledger.get("T1").status = "accepted"
        self.assertEqual(ledger.next_task().id, "T2")
        ledger.get("T2").status = "accepted"
        self.assertIsNone(ledger.next_task())
        self.assertTrue(ledger.is_complete())

    def test_failed_dependency_blocks_dependents(self):
        ledger = self.make(
            [
                {"id": "T1", "title": "iface", "file": "a.py"},
                {"id": "T2", "title": "caller", "file": "b.py", "depends_on": ["T1"]},
            ]
        )
        ledger.get("T1").status = "failed"
        self.assertIsNone(ledger.next_task())
        self.assertEqual([t.id for t in ledger.blocked_tasks()], ["T2"])

    def test_add_task_inserts_after(self):
        ledger = self.make(
            [
                {"id": "T1", "title": "one", "file": "a.py"},
                {"id": "T2", "title": "two", "file": "b.py"},
            ]
        )
        new_task = ledger.add_task({"title": "fix", "file": "a.py"}, after="T1")
        self.assertEqual([t.id for t in ledger.tasks], ["T1", new_task.id, "T2"])
        self.assertNotIn(new_task.id, ("T1", "T2"))

    def test_paths_are_stored_posix_style(self):
        ledger = self.make([{"title": "t", "file": "net\\client.py"}])
        self.assertEqual(ledger.tasks[0].file, "net/client.py")

    def test_save_and_load_roundtrip(self):
        with GitTemporaryDirectory() as root:
            path = Path(root) / ".aider.pipeline" / "ledger.yml"
            ledger = Ledger(path, request="add rate limiting")
            ledger.set_tasks([{"id": "T1", "title": "one", "file": "a.py"}])
            ledger.plan_summary = "a plan"
            ledger.memory.add("a fact", pinned=True)
            ledger.get("T1").status = "accepted"
            ledger.get("T1").commit = "abc123"
            ledger.log("REVIEW", task="T1", verdict="ACCEPT")
            ledger.save()

            loaded = Ledger.load(path)
            self.assertEqual(loaded.request, "add rate limiting")
            self.assertEqual(loaded.plan_summary, "a plan")
            self.assertEqual(loaded.get("T1").commit, "abc123")
            self.assertEqual(loaded.get("T1").status, "accepted")
            self.assertEqual(loaded.memory.items[0].text, "a fact")
            self.assertEqual(loaded.history[-1]["verdict"], "ACCEPT")

    def test_render_view_shrinks_to_fit(self):
        tasks = [
            {"id": f"T{i}", "title": f"task number {i}", "file": f"f{i}.py"} for i in range(1, 30)
        ]
        ledger = self.make(tasks)
        for task in ledger.tasks[:-1]:
            task.status = "accepted"
            task.notes = "a fairly long note about what this task ended up doing in the end"

        big = ledger.render_view()
        small = ledger.render_view(token_count=count, max_tokens=60)
        self.assertLess(count(small), count(big))
        self.assertLessEqual(count(small), 60)
        # The unfinished task always survives
        self.assertIn("T29", small)

    def test_render_view_never_includes_briefs(self):
        ledger = self.make([{"id": "T1", "title": "one", "file": "a.py"}])
        ledger.get("T1").brief_path = ".aider.pipeline/briefs/T1.md"
        self.assertNotIn("briefs", ledger.render_view())

    def test_totals(self):
        ledger = self.make([{"id": "T1", "title": "one", "file": "a.py"}])
        ledger.log("PLAN", tokens_in=100, tokens_out=10, seconds=2, role="architect")
        ledger.log("EDIT", tokens_in=50, tokens_out=80, seconds=5, role="worker")
        totals = ledger.totals()
        self.assertEqual(totals["calls"], 2)
        self.assertEqual(totals["worker_calls"], 1)
        self.assertEqual(totals["tokens_in"], 150)


class TestVerifyHelpers(unittest.TestCase):
    def test_clamp_drops_the_tail(self):
        text = "\n".join(f"line {i}" for i in range(100))
        trimmed = clamp(text, count, 20)
        self.assertLessEqual(count(trimmed), 25)
        self.assertTrue(trimmed.startswith("line 0"))

    def test_clamp_leaves_small_text_alone(self):
        self.assertEqual(clamp("short", count, 100), "short")

    def test_trim_test_output_keeps_summary_and_first_failure(self):
        lines = ["collecting ..."] + [f"noise line {i}" for i in range(400)]
        lines += [
            "FAILED tests/test_a.py::test_one",
            "E   AssertionError: expected 3 got 4",
        ]
        lines += [f"more noise {i}" for i in range(200)]
        lines += ["=== short test summary ===", "1 failed, 12 passed in 3.2s"]
        output = "\n".join(lines)

        trimmed = trim_test_output(output, count, 120)
        self.assertLessEqual(count(trimmed), 130)
        self.assertIn("1 failed, 12 passed", trimmed)
        self.assertIn("FAILED tests/test_a.py::test_one", trimmed)
        self.assertNotIn("more noise 100", trimmed)

    def test_trim_test_output_leaves_short_output_alone(self):
        self.assertEqual(trim_test_output("2 passed", count, 100), "2 passed")


if __name__ == "__main__":
    unittest.main()

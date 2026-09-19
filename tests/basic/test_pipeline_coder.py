import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import git

from aider.coders import Coder
from aider.coders.pipeline_coder import PipelineCoder
from aider.io import InputOutput
from aider.models import Model
from aider.pipeline.config import PipelineConfig
from aider.pipeline.worker import WorkerPool
from aider.utils import GitTemporaryDirectory

CALC_SOURCE = '''\
"""A tiny calculator."""


def add(a, b):
    return a + b


def scale(value, factor):
    return value * factor
'''

PLAN_REPLY = """\
We will make scale clamp its factor.

```yaml
plan_summary: |
  Clamp the factor in scale() so it is never negative.
tasks:
  - id: T1
    title: Clamp the factor in scale
    file: calc.py
    kind: edit
    symbols: [scale]
    depends_on: []
```
"""

BRIEF_REPLY = """\
# Task T1: Clamp the factor in scale
File to edit: calc.py   (this is the ONLY file you may change)

## Goal
scale() should treat a negative factor as zero.

## Changes
- `scale(value, factor)`: if factor < 0, use 0 instead. Return type unchanged.

## Acceptance criteria
- [ ] scale(5, -2) returns 0
"""

WORKER_EDIT = '''\
calc.py
```
"""A tiny calculator."""


def add(a, b):
    return a + b


def scale(value, factor):
    if factor < 0:
        factor = 0
    return value * factor
```
'''

ACCEPT_REPLY = """\
VERDICT: ACCEPT
NOTE: scale clamps negative factors to zero
The diff matches the brief.
REMEMBER: scale() clamps a negative factor to zero
"""

RETRY_REPLY = """\
VERDICT: RETRY
NOTE: the clamp is missing
The worker did not add the clamp.

# Task T1: Clamp the factor in scale
File to edit: calc.py   (this is the ONLY file you may change)

## Changes
- `scale(value, factor)`: really do add the clamp this time.

## Acceptance criteria
- [ ] scale(5, -2) returns 0
"""


class ScriptedArchitect:
    """Returns canned replies in order and records the prompts it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, messages):
        self.prompts.append(messages)
        if not self.replies:
            raise AssertionError("The architect was called more times than expected")
        return self.replies.pop(0)

    def prompt_text(self, index):
        return "\n".join(m["content"] for m in self.prompts[index])


class PipelineTestCase(unittest.TestCase):
    def make_coder(self, root, io=None, config=None, worker_replies=None, architect_replies=None):
        io = io or InputOutput(yes=True)
        architect_model = Model("gpt-4o")
        worker_model = Model("gpt-4o-mini")
        # Warm-up pings would consume the scripted replies.
        config = (config or PipelineConfig(approve="never")).replace(prewarm=False)

        coder = Coder.create(
            main_model=architect_model,
            edit_format="pipeline",
            io=io,
            fnames=[],
            use_git=True,
            auto_commits=True,
            pipeline_config=config,
            pipeline_worker_model=worker_model,
            stream=False,
        )
        self.assertIsInstance(coder, PipelineCoder)

        self.architect = ScriptedArchitect(architect_replies or [])
        architect_model.simple_send_with_retries = self.architect

        self.worker_replies = list(worker_replies or [])
        self.worker_calls = []
        self.patch_worker()
        return coder

    def patch_worker(self):
        """Let the real worker coder run, but script what the model returns."""
        original = WorkerPool.coder_for
        test = self

        def coder_for(pool, edit_format):
            coder = original(pool, edit_format)
            if not getattr(coder, "_scripted", False):

                def send(messages, functions=None):
                    test.worker_calls.append(messages)
                    coder.partial_response_content = (
                        test.worker_replies.pop(0) if test.worker_replies else ""
                    )
                    coder.partial_response_function_call = dict()
                    return []

                coder.send = send
                coder._scripted = True
            return coder

        patcher = patch.object(WorkerPool, "coder_for", coder_for)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestPipelineRun(PipelineTestCase):
    def test_happy_path_plans_edits_reviews_and_commits(self):
        with GitTemporaryDirectory() as root:
            calc = Path(root) / "calc.py"
            calc.write_text(CALC_SOURCE)
            repo = git.Repo(root)
            repo.git.add("calc.py")
            repo.git.commit("-m", "initial")

            coder = self.make_coder(
                root,
                architect_replies=[PLAN_REPLY, BRIEF_REPLY, ACCEPT_REPLY],
                worker_replies=[WORKER_EDIT],
            )
            coder.repo.get_commit_message = MagicMock(return_value="pipeline commit")

            coder.run(with_message="clamp the factor in scale", preproc=False)

            self.assertIn("if factor < 0", calc.read_text())

            task = coder.ledger.get("T1")
            self.assertEqual(task.status, "accepted")
            self.assertTrue(task.commit, "the accepted task should have a commit")
            self.assertEqual(task.notes, "scale clamps negative factors to zero")

            # One commit per accepted task, on top of the initial commit
            commits = list(repo.iter_commits(repo.active_branch.name))
            self.assertEqual(len(commits), 2)
            self.assertIn("pipeline T1", commits[0].message)

            self.assertTrue(coder.ledger_path.exists())
            self.assertIn(
                "scale() clamps a negative factor to zero",
                [item.text for item in coder.ledger.memory.items],
            )

    def test_architect_never_sees_file_contents(self):
        with GitTemporaryDirectory() as root:
            calc = Path(root) / "calc.py"
            calc.write_text(CALC_SOURCE)
            repo = git.Repo(root)
            repo.git.add("calc.py")
            repo.git.commit("-m", "initial")

            coder = self.make_coder(
                root,
                architect_replies=[PLAN_REPLY, BRIEF_REPLY, ACCEPT_REPLY],
                worker_replies=[WORKER_EDIT],
            )
            coder.repo.get_commit_message = MagicMock(return_value="c")
            coder.run(with_message="clamp the factor", preproc=False)

            plan_prompt = self.architect.prompt_text(0)
            self.assertNotIn('"""A tiny calculator."""', plan_prompt)
            self.assertIn("calc.py", plan_prompt)

            brief_prompt = self.architect.prompt_text(1)
            self.assertNotIn("return a + b", brief_prompt)
            # It gets an outline instead of the source
            self.assertIn("Outline of calc.py", brief_prompt)
            self.assertIn("scale", brief_prompt)

    def test_retry_then_accept_makes_one_commit(self):
        with GitTemporaryDirectory() as root:
            calc = Path(root) / "calc.py"
            calc.write_text(CALC_SOURCE)
            repo = git.Repo(root)
            repo.git.add("calc.py")
            repo.git.commit("-m", "initial")

            no_op_edit = WORKER_EDIT.replace("    if factor < 0:\n        factor = 0\n", "")
            coder = self.make_coder(
                root,
                architect_replies=[PLAN_REPLY, BRIEF_REPLY, RETRY_REPLY, ACCEPT_REPLY],
                worker_replies=[no_op_edit, WORKER_EDIT],
            )
            coder.repo.get_commit_message = MagicMock(return_value="c")
            coder.run(with_message="clamp the factor", preproc=False)

            task = coder.ledger.get("T1")
            self.assertEqual(task.status, "accepted")
            self.assertEqual(task.attempts, 2)
            commits = list(repo.iter_commits(repo.active_branch.name))
            self.assertEqual(len(commits), 2, "a retried task is still one commit")

    def test_task_fails_after_max_attempts(self):
        with GitTemporaryDirectory() as root:
            calc = Path(root) / "calc.py"
            calc.write_text(CALC_SOURCE)
            repo = git.Repo(root)
            repo.git.add("calc.py")
            repo.git.commit("-m", "initial")

            coder = self.make_coder(
                root,
                config=PipelineConfig(approve="never", max_attempts=2),
                architect_replies=[PLAN_REPLY, BRIEF_REPLY, RETRY_REPLY, RETRY_REPLY],
                worker_replies=[WORKER_EDIT, WORKER_EDIT],
            )
            coder.repo.get_commit_message = MagicMock(return_value="c")
            coder.run(with_message="clamp the factor", preproc=False)

            self.assertEqual(coder.ledger.get("T1").status, "failed")

    def test_bad_plan_is_re_asked_then_reported(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "calc.py").write_text(CALC_SOURCE)
            io = InputOutput(yes=True)
            io.tool_error = MagicMock()
            coder = self.make_coder(
                root,
                io=io,
                architect_replies=["I think we should probably refactor everything.", "Still prose."],
            )
            coder.run(with_message="do something", preproc=False)

            self.assertEqual(len(self.architect.prompts), 2)
            self.assertIn(
                "could not be used",
                " ".join(str(call) for call in io.tool_error.call_args_list),
            )

    def test_plan_is_resumable_from_the_ledger(self):
        with GitTemporaryDirectory() as root:
            calc = Path(root) / "calc.py"
            calc.write_text(CALC_SOURCE)
            repo = git.Repo(root)
            repo.git.add("calc.py")
            repo.git.commit("-m", "initial")

            io = InputOutput(yes=False)
            coder = self.make_coder(
                root,
                io=io,
                config=PipelineConfig(approve="plan"),
                architect_replies=[PLAN_REPLY],
                worker_replies=[WORKER_EDIT],
            )
            coder.run(with_message="clamp the factor", preproc=False)

            # Declining the plan leaves it on disk with nothing done
            self.assertEqual(coder.ledger.get("T1").status, "pending")
            self.assertTrue(coder.ledger_path.exists())

            self.architect.replies = [BRIEF_REPLY, ACCEPT_REPLY]
            coder.io = InputOutput(yes=True)
            coder.repo.get_commit_message = MagicMock(return_value="c")
            coder.resume()

            self.assertEqual(coder.ledger.get("T1").status, "accepted")
            self.assertIn("if factor < 0", calc.read_text())

    def test_stray_edits_are_reverted(self):
        with GitTemporaryDirectory() as root:
            calc = Path(root) / "calc.py"
            calc.write_text(CALC_SOURCE)
            other = Path(root) / "other.py"
            other.write_text("ORIGINAL = 1\n")
            repo = git.Repo(root)
            repo.git.add("calc.py", "other.py")
            repo.git.commit("-m", "initial")

            stray = WORKER_EDIT + "\nother.py\n```\nORIGINAL = 99\n```\n"
            coder = self.make_coder(
                root,
                architect_replies=[PLAN_REPLY, BRIEF_REPLY, ACCEPT_REPLY],
                worker_replies=[stray],
            )
            coder.repo.get_commit_message = MagicMock(return_value="c")
            coder.run(with_message="clamp the factor", preproc=False)

            self.assertEqual(other.read_text(), "ORIGINAL = 1\n")
            self.assertIn("if factor < 0", calc.read_text())

    def test_architect_prompt_stays_within_budget(self):
        with GitTemporaryDirectory() as root:
            calc = Path(root) / "calc.py"
            calc.write_text(CALC_SOURCE)
            for index in range(60):
                extra = Path(root) / f"mod{index}.py"
                extra.write_text(
                    "\n".join(
                        f"def function_{index}_{n}(argument_one, argument_two):\n"
                        f"    return argument_one + argument_two + {n}\n"
                        for n in range(20)
                    )
                )
            repo = git.Repo(root)
            repo.git.add(all=True)
            repo.git.commit("-m", "initial")

            config = PipelineConfig(approve="never")
            coder = self.make_coder(
                root,
                config=config,
                architect_replies=[PLAN_REPLY, BRIEF_REPLY, ACCEPT_REPLY],
                worker_replies=[WORKER_EDIT],
            )
            coder.repo.get_commit_message = MagicMock(return_value="c")
            coder.run(with_message="clamp the factor", preproc=False)

            budget = config.architect_prompt_budget
            for index, messages in enumerate(self.architect.prompts):
                tokens = coder.main_model.token_count(messages)
                self.assertLess(
                    tokens,
                    budget * 2,
                    f"architect prompt {index} was {tokens} tokens, budget is {budget}",
                )


class TestWorkerContextWipe(PipelineTestCase):
    def test_context_is_wiped_but_the_coder_is_reused(self):
        with GitTemporaryDirectory() as root:
            first = Path(root) / "one.py"
            first.write_text("VALUE = 1\n")
            second = Path(root) / "two.py"
            second.write_text("VALUE = 2\n")
            repo = git.Repo(root)
            repo.git.add(all=True)
            repo.git.commit("-m", "initial")

            two_task_plan = PLAN_REPLY.replace(
                "    depends_on: []",
                "    depends_on: []\n"
                "  - id: T2\n"
                "    title: Second task\n"
                "    file: two.py\n"
                "    kind: edit\n"
                "    depends_on: [T1]",
            ).replace("file: calc.py", "file: one.py")

            edit_one = "one.py\n```\nVALUE = 11\n```\n"
            edit_two = "two.py\n```\nVALUE = 22\n```\n"
            coder = self.make_coder(
                root,
                architect_replies=[
                    two_task_plan,
                    BRIEF_REPLY.replace("calc.py", "one.py"),
                    ACCEPT_REPLY,
                    BRIEF_REPLY.replace("calc.py", "two.py"),
                    ACCEPT_REPLY,
                ],
                worker_replies=[edit_one, edit_two],
            )
            coder.repo.get_commit_message = MagicMock(return_value="c")
            coder.run(with_message="bump both values", preproc=False)

            self.assertEqual(first.read_text(), "VALUE = 11\n")
            self.assertEqual(second.read_text(), "VALUE = 22\n")

            # One coder object served both tasks: no rebuild between hand-offs
            self.assertEqual(len(coder.workers.coders), 1)

            # The second task's prompt carries no trace of the first
            second_prompt = "\n".join(m["content"] for m in self.worker_calls[1])
            self.assertNotIn("one.py", second_prompt)
            self.assertNotIn("VALUE = 11", second_prompt)
            self.assertIn("two.py", second_prompt)

    def test_reset_clears_state_without_rebuilding(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("A = 1\n")
            io = InputOutput(yes=True)
            coder = Coder.create(
                main_model=Model("gpt-4o"),
                edit_format="pipeline",
                io=io,
                pipeline_worker_model=Model("gpt-4o-mini"),
                pipeline_config=PipelineConfig(prewarm=False),
            )
            pool = WorkerPool(coder, coder.worker_model, coder.config)
            worker = pool.coder_for("pipeline-worker-whole")
            again = pool.coder_for("pipeline-worker-whole")
            self.assertIs(worker, again, "the worker coder should be reused")

            worker.done_messages = [{"role": "user", "content": "old"}]
            worker.cur_messages = [{"role": "user", "content": "older"}]
            worker.abs_fnames = {"/tmp/stale.py"}
            worker.aider_edited_files = {"stale.py"}
            worker.num_reflections = 3

            pool.reset(worker, ["/tmp/fresh.py"])

            self.assertEqual(worker.done_messages, [])
            self.assertEqual(worker.cur_messages, [])
            self.assertEqual(worker.abs_fnames, {"/tmp/fresh.py"})
            self.assertEqual(worker.aider_edited_files, set())
            self.assertEqual(worker.num_reflections, 0)

    def test_worker_system_prompt_is_identical_between_tasks(self):
        """A stable prefix is what lets a local server reuse its KV cache."""
        with GitTemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("A = 1\n")
            (Path(root) / "b.py").write_text("B = 2\n")
            coder = Coder.create(
                main_model=Model("gpt-4o"),
                edit_format="pipeline",
                io=InputOutput(yes=True),
                pipeline_worker_model=Model("gpt-4o-mini"),
                pipeline_config=PipelineConfig(prewarm=False),
            )
            pool = WorkerPool(coder, coder.worker_model, coder.config)
            worker = pool.coder_for("pipeline-worker-whole")

            prompts = []
            for name in ("a.py", "b.py"):
                pool.reset(worker, [str(Path(root) / name)])
                worker.cur_messages = [{"role": "user", "content": "brief"}]
                prompts.append(worker.format_messages().all_messages()[0]["content"])

            self.assertEqual(prompts[0], prompts[1])

    def test_worker_does_not_pull_in_more_files(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("A = 1\n")
            (Path(root) / "elsewhere.py").write_text("B = 2\n")
            coder = Coder.create(
                main_model=Model("gpt-4o"),
                edit_format="pipeline",
                io=InputOutput(yes=True),
                pipeline_worker_model=Model("gpt-4o-mini"),
                pipeline_config=PipelineConfig(prewarm=False),
            )
            pool = WorkerPool(coder, coder.worker_model, coder.config)
            worker = pool.coder_for("pipeline-worker-whole")
            self.assertIsNone(worker.check_for_file_mentions("please add elsewhere.py"))

    def test_prewarm_is_off_when_disabled(self):
        with GitTemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("A = 1\n")
            coder = Coder.create(
                main_model=Model("gpt-4o"),
                edit_format="pipeline",
                io=InputOutput(yes=True),
                pipeline_worker_model=Model("gpt-4o-mini"),
                pipeline_config=PipelineConfig(prewarm=False),
            )
            pool = WorkerPool(coder, coder.worker_model, coder.config)
            pool.prewarm()
            self.assertIsNone(pool.prewarm_thread)

    def test_pick_format_switches_on_big_files(self):
        with GitTemporaryDirectory() as root:
            small = Path(root) / "small.py"
            small.write_text("A = 1\n")
            big = Path(root) / "big.py"
            big.write_text("\n".join(f"VALUE_{n} = {n}" for n in range(4000)))
            coder = Coder.create(
                main_model=Model("gpt-4o"),
                edit_format="pipeline",
                io=InputOutput(yes=True),
                pipeline_worker_model=Model("gpt-4o-mini"),
                pipeline_config=PipelineConfig(prewarm=False),
            )
            pool = WorkerPool(coder, coder.worker_model, coder.config)
            self.assertEqual(pool.pick_format(str(small)), "pipeline-worker-whole")
            self.assertEqual(pool.pick_format(str(big)), "pipeline-worker-diff")


class TestPipelineOffByDefault(unittest.TestCase):
    def test_default_coder_is_unchanged(self):
        with GitTemporaryDirectory():
            coder = Coder.create(main_model=Model("gpt-4o"), io=InputOutput(yes=True))
            self.assertNotEqual(coder.edit_format, "pipeline")
            self.assertIsNone(coder.pipeline_worker_model)
            self.assertIsInstance(coder.pipeline_config, PipelineConfig)

    def test_switching_away_from_pipeline_works(self):
        with GitTemporaryDirectory():
            coder = Coder.create(
                main_model=Model("gpt-4o"),
                edit_format="pipeline",
                io=InputOutput(yes=True),
                pipeline_worker_model=Model("gpt-4o-mini"),
            )
            other = Coder.create(from_coder=coder, edit_format="diff")
            self.assertEqual(other.edit_format, "diff")

            back = Coder.create(from_coder=other, edit_format="pipeline")
            self.assertEqual(back.edit_format, "pipeline")
            self.assertEqual(back.worker_model.name, "gpt-4o-mini")

    def test_worker_defaults_to_the_editor_model(self):
        with GitTemporaryDirectory():
            model = Model("gpt-4o", editor_model="gpt-4o-mini")
            coder = Coder.create(
                main_model=model, edit_format="pipeline", io=InputOutput(yes=True)
            )
            self.assertEqual(coder.worker_model.name, "gpt-4o-mini")

    def test_worker_falls_back_to_the_main_model(self):
        with GitTemporaryDirectory():
            coder = Coder.create(
                main_model=Model("gpt-4o"), edit_format="pipeline", io=InputOutput(yes=True)
            )
            self.assertEqual(coder.worker_model.name, "gpt-4o")


if __name__ == "__main__":
    unittest.main()

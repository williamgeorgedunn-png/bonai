from pathlib import Path

from aider.pipeline import briefs, parsing, verify
from aider.pipeline.architect import ArchitectClient
from aider.pipeline.knowledge import KnowledgeService
from aider.pipeline.ledger import Ledger, LedgerError
from aider.pipeline.worker import WorkerPool

from .base_coder import Coder
from .pipeline_prompts import PipelinePrompts

PIPELINE_DIR = ".aider.pipeline"
REVIEW_VERDICTS = ("ACCEPT", "RETRY", "REPLAN")
TRIAGE_VERDICTS = ("FIX_CODE", "FIX_TEST", "ACCEPT_KNOWN", "ESCALATE")


class PipelineCoder(Coder):
    """Architect plans and reviews; a small worker edits one file at a time.

    The architect is stateless: each call is rebuilt from the ledger, working
    memory, and this step's inputs. PLAN and BRIEF use outlines, not source.
    REVIEW is sent a token-capped git diff of the one file that changed;
    NEED: source can pull one symbol. Nothing accumulates in chat history.
    The worker gets an empty context for every task. State lives in a ledger
    on disk, so a run survives a crash and can be resumed.
    """

    edit_format = "pipeline"
    gpt_prompts = PipelinePrompts()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.pipeline_stop = False
        self.pipeline_running = False

        self.config = self.pipeline_config
        problems = self.config.validate()
        for problem in problems:
            self.io.tool_warning(problem)

        self.worker_model = (
            self.pipeline_worker_model or self.main_model.editor_model or self.main_model
        )

        if self.repo_map:
            # PLAN/BRIEF use the map and outlines, not chat file dumps.
            self.repo_map.max_map_tokens = self.config.architect_map_tokens
            self.repo_map.map_mul_no_files = 1.0

        self.ledger = None
        self.knowledge = None
        self.architect = None
        self.workers = None

    # ---------------------------------------------------------------- setup

    @property
    def ledger_path(self):
        return Path(self.root) / PIPELINE_DIR / "ledger.yml"

    def announce_pipeline(self):
        lines = [
            f"Pipeline mode: architect {self.main_model.name}, worker {self.worker_model.name}",
            f"Budgets: map {self.config.architect_map_tokens}, memory"
            f" {self.config.working_memory_tokens}, facts {self.config.facts_tokens} tokens",
            f"Approvals: {self.config.approve}",
        ]
        if self.config.prewarm:
            lines.append(
                "Prewarm is on: a real request will be sent to both models at startup"
            )
        for line in lines:
            self.io.tool_output(line)

    def get_announcements(self):
        lines = super().get_announcements()
        lines.append(f"Pipeline worker: {self.worker_model.name}")
        return lines

    def setup_run(self, ledger):
        """Wire up the services for one run."""
        self.ledger = ledger
        self.workers = WorkerPool(self, self.worker_model, self.config, ledger=ledger)
        self.knowledge = KnowledgeService(
            self.root,
            self.io,
            self.config,
            repo_map=self.repo_map,
            tracer=self.tracer,
            token_count=self.main_model.token_count,
            digest_fn=self.workers.digest,
            get_all_abs_files=self.get_all_abs_files,
            cache_dir=Path(self.root) / PIPELINE_DIR / "digests",
        )
        self.architect = ArchitectClient(
            self.main_model,
            self.io,
            self.config,
            ledger,
            self.gpt_prompts,
            knowledge=self.knowledge,
            repo_map_fn=self.repo_map_for,
        )
        self.architect.verbose = self.verbose
        self.workers.prewarm()

    def repo_map_for(self, task):
        if not self.repo_map:
            return None
        all_abs = set(self.get_all_abs_files())
        chat_files = set(self.abs_fnames)
        mentioned_fnames = set()
        mentioned_idents = set()
        if task:
            abs_fname = self.abs_root_path(task.file)
            if abs_fname in all_abs:
                chat_files.add(abs_fname)
            mentioned_fnames.add(task.file)
            mentioned_idents.update(task.symbols)
        other_files = all_abs - chat_files
        try:
            return self.repo_map.get_repo_map(
                chat_files,
                other_files,
                mentioned_fnames=mentioned_fnames,
                mentioned_idents=mentioned_idents,
            )
        except Exception as err:
            if self.verbose:
                self.io.tool_warning(f"Repo map unavailable: {err}")
            return None

    # ------------------------------------------------------------ run entry

    def run_one(self, user_message, preproc):
        self.init_before_message()

        if preproc:
            message = self.preproc_user_input(user_message)
        else:
            message = user_message

        if not message or not message.strip():
            return

        self.start(message)

    def start(self, request):
        """Plan and execute a request end to end."""
        if self.pipeline_running:
            self.io.tool_error("A pipeline run is already in progress.")
            return

        ledger = Ledger(self.ledger_path, request=request, config=self.config)
        ledger.memory.token_count = self.main_model.token_count
        self.setup_run(ledger)
        self.announce_pipeline()

        if not self.plan():
            return
        self.execute()

    def resume(self):
        """Continue a run from the ledger on disk."""
        if not self.ledger_path.exists():
            self.io.tool_error(f"No pipeline ledger at {self.ledger_path}")
            return
        try:
            ledger = Ledger.load(self.ledger_path, config=self.config)
        except (LedgerError, OSError, ValueError) as err:
            self.io.tool_error(f"Could not read the ledger: {err}")
            return
        ledger.memory.token_count = self.main_model.token_count
        self.setup_run(ledger)
        self.announce_pipeline()
        self.io.tool_output(f"Resuming: {len(ledger.tasks)} task(s) in the ledger.")
        self.execute()

    # ----------------------------------------------------------------- plan

    def plan(self):
        instruction = self.gpt_prompts.plan_step.format(max_tasks=self.config.max_tasks)
        inputs = None

        for attempt in range(1, 3):
            result = self.architect.run_step("PLAN", instruction, inputs=inputs)
            if not result.ok:
                self.io.tool_error(f"Architect failed to plan: {result.error}")
                return False

            if result.questions and self.answer_questions(result.questions):
                inputs = self.clarification_inputs()
                continue

            try:
                data = parsing.extract_yaml(result.text)
                raw_tasks = data.get("tasks") if isinstance(data, dict) else data
                if isinstance(data, dict) and data.get("plan_summary"):
                    self.ledger.plan_summary = str(data["plan_summary"]).strip()
                if not self.ledger.plan_summary:
                    self.ledger.plan_summary = result.body.split("```")[0].strip()[:1000]
                self.ledger.set_tasks(raw_tasks or [], max_tasks=self.config.max_tasks)
            except (ValueError, LedgerError) as err:
                if attempt == 2:
                    self.io.tool_error(f"The plan could not be used: {err}")
                    return False
                self.io.tool_warning(f"Re-asking the architect: {err}")
                inputs = (
                    "# Your previous plan could not be used\n"
                    f"{err}\n"
                    "Reply again with a single valid yaml block."
                )
                continue

            if self.config.tdd:
                self.apply_tdd_order()

            self.ledger.save()
            return self.approve_plan()

        return False

    def clarification_inputs(self):
        lines = ["# Answers from the user"]
        lines += [f"- {c}" for c in self.ledger.clarifications]
        return "\n".join(lines)

    def answer_questions(self, questions):
        """Ask the user the architect's questions. True if anything was answered."""
        if self.config.approve == "never":
            for question in questions:
                self.ledger.clarifications.append(
                    f"{question} -> (no human available; use your best judgement"
                    " and note the assumption)"
                )
            return True

        answered = False
        for question in questions:
            self.io.tool_output()
            answer = self.io.prompt_ask(f"Architect asks: {question}\n> ").strip()
            if answer:
                self.ledger.clarifications.append(f"{question} -> {answer}")
                answered = True
        return answered

    def apply_tdd_order(self):
        """Run test tasks before the code they cover."""
        for task in self.ledger.tasks:
            if task.kind != "test":
                continue
            covered = list(task.depends_on)
            task.depends_on = []
            task.notes = "TDD: written before the implementation, so it should fail first."
            for dep_id in covered:
                dep = self.ledger.get(dep_id)
                if dep is not None and task.id not in dep.depends_on:
                    dep.depends_on.append(task.id)
        try:
            self.ledger.validate_dag()
        except LedgerError as err:
            self.io.tool_warning(f"Could not apply TDD ordering: {err}")

    def approve_plan(self):
        self.show_plan()
        if self.config.approve == "never":
            return True
        if not self.io.confirm_ask("Work through this plan?"):
            self.io.tool_output(
                f"Stopped. The plan is saved at {self.ledger_path}; edit it and run"
                " /pipeline resume."
            )
            return False
        return True

    def show_plan(self):
        self.io.tool_output()
        if self.ledger.plan_summary:
            self.io.tool_output(self.ledger.plan_summary)
            self.io.tool_output()
        for task in self.ledger.tasks:
            deps = f" after {', '.join(task.depends_on)}" if task.depends_on else ""
            status = "" if task.status == "pending" else f" [{task.status}]"
            self.io.tool_output(f"  {task.id}{status} {task.file}: {task.title}{deps}")
        self.io.tool_output()

    # -------------------------------------------------------------- execute

    def execute(self):
        self.pipeline_running = True
        self.pipeline_stop = False
        try:
            while True:
                if self.pipeline_stop:
                    self.io.tool_output("Pipeline stopped.")
                    break
                if self.workers.budget_exhausted():
                    self.io.tool_error(
                        f"Stopping: hit the worker call limit ({self.config.max_worker_calls})."
                    )
                    break

                task = self.ledger.next_task()
                if task is None:
                    break

                try:
                    self.do_task(task)
                except KeyboardInterrupt:
                    self.io.tool_warning("\nInterrupted. The ledger is saved.")
                    break
                finally:
                    self.ledger.save()
        finally:
            self.pipeline_running = False
            self.ledger.save()
            self.report()

    def do_task(self, task):
        self.io.tool_output()
        self.io.tool_output(f"--- {task.id} {task.file}: {task.title}")

        brief = None
        while True:
            task.attempts += 1
            if task.attempts > self.config.max_attempts:
                self.fail_task(task, "out of attempts")
                return

            # A RETRY verdict already carries a revised brief, so don't pay for
            # another architect call to get one.
            if not brief:
                brief = self.get_brief(task)
            if not brief:
                self.fail_task(task, "no brief")
                return

            if self.config.approve == "task":
                self.io.tool_output(brief)
                if not self.io.confirm_ask(f"Send {task.id} to the worker?"):
                    task.status = "skipped"
                    return

            task.status = "editing"
            result = self.edit_task(task, brief)
            if not result.ok and not result.edited_files:
                self.io.tool_warning(f"{task.id}: {result.error}")
                if task.attempts >= self.config.max_attempts:
                    self.fail_task(task, result.error)
                    return
                continue

            task.status = "review"
            verdict, brief = self.review_task(task, brief, result)

            if verdict == "ACCEPT":
                self.accept_task(task)
                self.maybe_test(task)
                return
            if verdict == "REPLAN":
                self.io.tool_output(f"{task.id}: architect asked to replan.")
                task.status = "skipped"
                return
            if verdict == "REVIEW_FAILED":
                self.fail_task(task, "the architect could not review the change")
                return
            if task.attempts >= self.config.max_attempts:
                self.fail_task(task, "the architect rejected the last attempt")
                return
            self.io.tool_output(f"{task.id}: retrying with a revised brief.")

    # --------------------------------------------------------------- briefs

    def get_brief(self, task):
        """Brief from the architect, with read-only snippets inlined."""
        saved = briefs.load_brief(self.root, task.id)
        if saved and task.attempts == 1 and task.status == "briefed":
            return saved

        instruction = self.brief_instruction(task)
        inputs = self.brief_inputs(task)
        step = "TEST_BRIEF" if task.kind == "test" else "BRIEF"
        result = self.architect.run_step(step, instruction, inputs=inputs, task=task)
        if not result.ok:
            self.io.tool_error(f"Architect failed to brief {task.id}: {result.error}")
            return ""

        return self.finish_brief(task, result.body)

    def finish_brief(self, task, text):
        brief = briefs.clean_brief(text, self.main_model.reasoning_tag)
        if not brief:
            return ""
        brief, resolved = briefs.resolve_snippets(
            brief,
            self.knowledge,
            self.config.worker_snippet_tokens,
            token_count=self.worker_model.token_count,
        )
        if resolved and self.verbose:
            self.io.tool_output(f"[pipeline] snippets for {task.id}: {', '.join(resolved)}")
        path = briefs.save_brief(self.root, task.id, brief)
        task.brief_path = str(path.relative_to(self.root)).replace("\\", "/")
        task.status = "briefed"
        self.ledger.save()
        return brief

    def brief_instruction(self, task):
        if task.kind == "test":
            return self.gpt_prompts.test_brief_step.format(
                task_id=task.id,
                task_title=task.title,
                task_file=task.file,
                test_cmd=self.test_cmd or "(no test command configured)",
            )
        return self.gpt_prompts.brief_step.format(
            task_id=task.id,
            task_title=task.title,
            task_file=task.file,
        )

    def brief_inputs(self, task):
        """The outline of the target file, so the architect need not read it."""
        parts = []
        if task.symbols:
            parts.append(f"# Symbols this task should touch\n{', '.join(task.symbols)}")

        abs_fname = self.abs_root_path(task.file)
        if Path(abs_fname).exists():
            from aider.pipeline.parsing import Need

            fact = self.knowledge.answer(Need("outline", target=task.file))
            if fact:
                parts.append(f"# Outline of {task.file}\n{fact.text}")
        else:
            parts.append(f"# {task.file} does not exist yet; the worker will create it.")

        return "\n\n".join(parts)

    # ----------------------------------------------------------------- edit

    def edit_task(self, task, brief):
        abs_fname = self.abs_root_path(task.file)
        message = briefs.worker_message(brief)

        self.io.tool_output(f"Worker editing {task.file} ...")
        result = self.workers.edit(abs_fname, message)
        if result.seconds:
            self.io.tool_output(f"Worker finished in {result.seconds:.0f}s")

        stray = {f for f in result.edited_files if f != task.file}
        if stray and self.repo:
            reverted = verify.revert_files(self.repo, stray, io=self.io)
            if reverted:
                self.io.tool_warning(
                    f"Reverted edits outside {task.file}: {', '.join(sorted(reverted))}"
                )
                result.error = (
                    f"The worker also edited {', '.join(sorted(reverted))}, which was"
                    " reverted. Only the task's file may change."
                )
        return result

    # --------------------------------------------------------------- review

    def review_task(self, task, brief, result):
        """Check the diff against the brief. Returns (verdict, next brief)."""
        lint_output = ""
        abs_fname = self.abs_root_path(task.file)
        if Path(abs_fname).exists():
            lint_output = verify.lint_file(self, abs_fname)

        diff = verify.file_diff(self.repo, self.root, task.file, read_text=self.io.read_text)
        diff = self.shrink_diff(diff)

        inputs = [f"# Brief you wrote for {task.id}\n{brief}"]
        inputs.append(f"# Diff of {task.file}\n```diff\n{diff or '(no changes detected)'}\n```")
        if lint_output:
            trimmed = verify.clamp(
                lint_output, self.main_model.token_count, self.config.lint_output_tokens
            )
            inputs.append(f"# Lint output\n{trimmed}")
        if result.error:
            inputs.append(f"# Problem reported by the orchestrator\n{result.error}")

        instruction = self.gpt_prompts.review_step.format(
            task_id=task.id,
            attempt=task.attempts,
            max_attempts=self.config.max_attempts,
        )
        step = self.architect.run_step(
            "REVIEW", instruction, inputs="\n\n".join(inputs), task=task
        )
        if not step.ok:
            self.io.tool_error(f"Architect failed to review {task.id}: {step.error}")
            return "REVIEW_FAILED", brief

        verdict = step.verdict or parsing.find_verdict(step.text, REVIEW_VERDICTS) or "ACCEPT"
        if step.directives and step.directives.notes:
            task.notes = step.directives.notes[0][:200]

        self.ledger.log("REVIEW", task=task.id, verdict=verdict)
        self.io.tool_output(f"Architect review of {task.id}: {verdict}")

        next_brief = None
        if verdict == "RETRY":
            next_brief = self.finish_brief(task, step.body) or None
        elif verdict == "REPLAN":
            self.apply_replan(step, task)

        if self.config.approve == "task" and verdict != "ACCEPT":
            if not self.io.confirm_ask(f"Architect said {verdict} for {task.id}. Continue?"):
                self.pipeline_stop = True

        return verdict, next_brief

    def shrink_diff(self, diff):
        """Keep review input small; summarise through the worker if needed."""
        if not diff:
            return diff
        if self.main_model.token_count(diff) <= self.config.review_diff_tokens:
            return diff

        summary = self.workers.summarise_diff(diff)
        head = verify.clamp(
            diff, self.main_model.token_count, self.config.review_diff_tokens // 2
        )
        if summary:
            return f"(diff summarised by the worker)\n{summary}\n\n(first part of the diff)\n{head}"
        return head

    def apply_replan(self, step, task):
        try:
            data = parsing.extract_yaml(step.text)
        except ValueError:
            self.io.tool_warning("The architect asked to replan but sent no task list.")
            return
        raw_tasks = data.get("tasks") if isinstance(data, dict) else data
        if not isinstance(raw_tasks, list):
            return
        after = task.id
        for raw in raw_tasks:
            try:
                new_task = self.ledger.add_task(raw, after=after)
            except LedgerError as err:
                self.io.tool_warning(f"Ignoring a replanned task: {err}")
                continue
            after = new_task.id
            self.io.tool_output(f"Added {new_task.id} {new_task.file}: {new_task.title}")

    # --------------------------------------------------------------- commit

    def accept_task(self, task):
        task.status = "accepted"
        abs_fname = self.abs_root_path(task.file)
        self.aider_edited_files.add(task.file)

        if self.dry_run or not self.repo:
            self.io.tool_output(f"{task.id} accepted (no commit: dry run or no git repo).")
            return

        message = f"pipeline {task.id}: {task.title}"
        try:
            result = self.repo.commit(
                fnames=[abs_fname], message=message, aider_edits=True, coder=self
            )
        except Exception as err:
            self.io.tool_warning(f"Could not commit {task.id}: {err}")
            return

        if result:
            commit_hash = result[0] if isinstance(result, (tuple, list)) else result
            task.commit = str(commit_hash)
            self.aider_commit_hashes.add(task.commit)
            self.io.tool_output(f"{task.id} committed as {task.commit}")

    def fail_task(self, task, reason):
        task.status = "failed"
        self.io.tool_error(f"{task.id} failed: {reason}")
        blocked = self.ledger.blocked_tasks()
        if blocked:
            self.io.tool_warning(
                "Blocked by that failure: " + ", ".join(t.id for t in blocked)
            )
        if self.config.approve != "never":
            if not self.io.confirm_ask("Keep going with the rest of the plan?"):
                self.pipeline_stop = True

    # ---------------------------------------------------------------- tests

    def maybe_test(self, task):
        if not self.test_cmd:
            return
        # A task carrying test rounds came out of a triage, so re-run the tests
        # it was meant to fix.
        if task.kind != "test" and not self.auto_test and not task.test_rounds:
            return

        self.io.tool_output("Running tests ...")
        exit_status, output = verify.run_tests(
            self.test_cmd, self.root, verbose=self.verbose, error_print=self.io.tool_error
        )
        if not exit_status:
            self.io.tool_output("Tests passed.")
            return

        if self.config.tdd and task.kind == "test" and task.test_rounds == 0:
            self.io.tool_output("Tests fail as expected for a TDD task; carrying on.")
            task.test_rounds += 1
            return

        self.triage_tests(task, output)

    def triage_tests(self, task, output):
        task.test_rounds += 1
        if task.test_rounds > self.config.max_test_rounds:
            self.io.tool_error(f"{task.id}: giving up after {task.test_rounds} test rounds.")
            return

        trimmed = verify.trim_test_output(
            output, self.main_model.token_count, self.config.test_output_tokens
        )
        instruction = self.gpt_prompts.triage_step.format(
            task_id=task.id,
            round=task.test_rounds,
            max_rounds=self.config.max_test_rounds,
        )
        inputs = f"# Test output\n```\n{trimmed}\n```"
        step = self.architect.run_step("TRIAGE", instruction, inputs=inputs, task=task)
        if not step.ok:
            self.io.tool_error(f"Architect failed to triage tests: {step.error}")
            return

        verdict = step.verdict or parsing.find_verdict(step.text, TRIAGE_VERDICTS) or "ESCALATE"
        self.ledger.log("TRIAGE", task=task.id, verdict=verdict)
        self.io.tool_output(f"Architect triage: {verdict}")

        if verdict in ("ACCEPT_KNOWN",):
            note = (step.directives.notes or ["Known failure accepted."])[0][:200]
            if self.config.approve == "never":
                # Unattended mode has no human to confirm a leftover failure.
                self.io.tool_error(
                    "The architect accepted a known test failure, but"
                    " --pipeline-approve never has no one to confirm it."
                    " Treating this as ESCALATE."
                )
                task.notes = note
                self.pipeline_stop = True
                return
            task.notes = note
            return
        if verdict == "ESCALATE":
            self.io.tool_error("The architect escalated the test failure to you.")
            self.pipeline_stop = True
            return

        self.insert_fix_task(task, step)

    def insert_fix_task(self, task, step):
        """A failing test becomes a new task with its own commit, never an amend."""
        try:
            data = parsing.extract_yaml(step.text)
        except ValueError:
            self.io.tool_warning("The architect asked for a fix but sent no task.")
            return
        raw_tasks = data.get("tasks") if isinstance(data, dict) else data
        if isinstance(raw_tasks, dict):
            raw_tasks = [raw_tasks]
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return

        try:
            new_task = self.ledger.add_task(raw_tasks[0], after=task.id)
        except LedgerError as err:
            self.io.tool_warning(f"Could not add the fix task: {err}")
            return

        new_task.test_rounds = task.test_rounds
        brief = self.finish_brief(new_task, step.body)
        if not brief:
            new_task.status = "pending"
        self.io.tool_output(f"Added {new_task.id} {new_task.file}: {new_task.title}")

    # --------------------------------------------------------------- report

    def report(self):
        if not self.ledger:
            return
        counts = self.ledger.counts()
        totals = self.ledger.totals()
        parts = [f"{count} {status}" for status, count in sorted(counts.items())]
        self.io.tool_output()
        self.io.tool_output("Pipeline: " + (", ".join(parts) or "nothing to do"))
        self.io.tool_output(
            f"Calls: {totals['calls']} ({totals['worker_calls']} worker),"
            f" ~{totals['tokens_in']:,} tokens in, ~{totals['tokens_out']:,} out,"
            f" {totals['seconds']:.0f}s of model time"
        )
        if self.knowledge:
            self.io.tool_output(
                f"Digests: {self.knowledge.digest_calls} written,"
                f" {self.knowledge.digest_hits} reused from cache"
            )
        memory = self.ledger.memory
        self.io.tool_output(
            f"Working memory: {len(memory.items)} item(s), ~{memory.total_tokens()}"
            f" of {memory.max_tokens} tokens, {memory.evicted} evicted"
        )
        if not self.ledger.is_complete():
            self.io.tool_output("Run /pipeline resume to continue.")

    # -------------------------------------------------------------- command

    def status(self):
        if not self.ledger:
            if self.ledger_path.exists():
                try:
                    self.ledger = Ledger.load(self.ledger_path, config=self.config)
                except (LedgerError, OSError, ValueError) as err:
                    self.io.tool_error(f"Could not read the ledger: {err}")
                    return
            else:
                self.io.tool_output("No pipeline run yet.")
                return
        self.show_plan()
        self.report()

    def skip(self, task_id):
        return self.set_status(task_id, "skipped")

    def retry(self, task_id):
        return self.set_status(task_id, "pending", reset_attempts=True)

    def set_status(self, task_id, status, reset_attempts=False):
        if not self.ledger:
            self.io.tool_error("No pipeline run to change. Start one or /pipeline resume.")
            return False
        task = self.ledger.get(task_id)
        if task is None:
            self.io.tool_error(f"No task {task_id} in the ledger.")
            return False
        task.status = status
        if reset_attempts:
            task.attempts = 0
        self.ledger.save()
        self.io.tool_output(f"{task.id} is now {status}.")
        return True

    def abort(self):
        self.pipeline_stop = True
        self.io.tool_output("The pipeline will stop after the current step.")

    def digest_paths(self, paths):
        """Pre-compute worker digests for some files (optional bootstrap)."""
        if not self.workers:
            self.setup_run(Ledger(self.ledger_path, config=self.config))

        from aider.pipeline.parsing import Need

        rels = []
        for path in paths:
            match = self.knowledge._match_file(path) or path
            rels.append(match)

        index = self.knowledge.index()
        targets = []
        for rel_fname in rels:
            scopes = (index.scopes.get(rel_fname) if index else None) or []
            if scopes:
                for scope in scopes:
                    targets.append((rel_fname, index.qualified_name(scope)))
            else:
                targets.append((rel_fname, ""))

        if not targets:
            self.io.tool_output("Nothing to digest.")
            return

        self.io.tool_output(
            f"{len(targets)} symbol(s) to digest. Cached ones are free; the rest cost one"
            " worker call each."
        )
        if not self.io.confirm_ask("Run the digest pass?"):
            return

        try:
            for rel_fname, symbol in targets:
                need = Need("digest", target=rel_fname, symbol=symbol)
                self.knowledge.answer(need)
        except KeyboardInterrupt:
            self.io.tool_warning("\nStopped. Digests already written are kept.")

        self.io.tool_output(
            f"Digests: {self.knowledge.digest_calls} written,"
            f" {self.knowledge.digest_hits} reused."
        )

    def edit_ledger(self):
        if not self.ledger_path.exists():
            self.io.tool_error(f"No ledger at {self.ledger_path}")
            return
        from aider.editor import pipe_editor

        current = self.ledger_path.read_text(encoding="utf-8")
        updated = pipe_editor(current, suffix="yml")
        if updated and updated != current:
            self.ledger_path.write_text(updated, encoding="utf-8")
            self.io.tool_output("Ledger updated. Run /pipeline resume to continue.")

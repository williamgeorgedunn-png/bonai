import threading
import time

WHOLE_FORMAT = "pipeline-worker-whole"
DIFF_FORMAT = "pipeline-worker-diff"

DIGEST_SYSTEM = """Summarise the following code for an engineer who will not read it.
Use exactly this format and at most 120 tokens:
Purpose: <1 line>
Inputs/outputs: <1-2 lines>
Side effects/deps: <1 line>
Gotchas: <0-1 line, omit if none>
Do not speculate about code you cannot see. Do not repeat the code."""

SUMMARISE_SYSTEM = """Summarise this diff for a reviewer who cannot see it.
One bullet per changed function or block: what changed and whether behaviour changed.
Be factual and brief. Do not suggest improvements."""


class EditResult:
    def __init__(self, ok, edited_files=None, reply="", error="", seconds=0.0):
        self.ok = ok
        self.edited_files = set(edited_files or ())
        self.reply = reply
        self.error = error
        self.seconds = seconds


class WorkerPool:
    """Runs worker tasks with a fresh context but a warm model.

    Each task gets an empty message history (the "context wipe"), but the
    coder object, model, git repo and HTTP session are reused. Rebuilding a
    Coder per task would re-scan the repo and reload model metadata, and
    changing the system prompt would throw away the server's prefix KV cache;
    both make every hand-off feel like a cold start.
    """

    def __init__(self, parent_coder, worker_model, config, ledger=None):
        self.parent = parent_coder
        self.io = parent_coder.io
        self.model = worker_model
        self.config = config
        self.ledger = ledger
        self.coders = {}
        self.calls = 0
        self.prewarm_thread = None

    # ------------------------------------------------------------ lifecycle

    def coder_for(self, edit_format):
        """Reuse one coder per edit format; build it at most once per run."""
        coder = self.coders.get(edit_format)
        if coder is not None:
            return coder

        from aider.coders.base_coder import Coder
        from aider.history import ChatSummary

        # The worker's history is wiped every task so it never summarizes, but
        # give it a summarizer anyway: a worker model configured without a weak
        # model would otherwise fail to build one.
        summarizer = ChatSummary([self.model], self.model.max_chat_history_tokens)

        coder = Coder.create(
            main_model=self.model,
            edit_format=edit_format,
            io=self.io,
            repo=self.parent.repo,
            fnames=[],
            read_only_fnames=[],
            map_tokens=0,
            auto_commits=False,
            dirty_commits=False,
            auto_lint=False,
            auto_test=False,
            dry_run=self.parent.dry_run,
            verbose=self.parent.verbose,
            stream=self.config.stream_worker and self.parent.stream,
            suggest_shell_commands=False,
            detect_urls=False,
            cache_prompts=False,
            num_cache_warming_pings=0,
            trace=False,
            auto_trace=False,
            total_cost=0.0,
            summarizer=summarizer,
        )
        coder.max_reflections = 1
        self.coders[edit_format] = coder
        return coder

    def pick_format(self, abs_fname):
        """Whole-file rewrites are far more reliable, but slow on big files."""
        text = self.io.read_text(abs_fname) or ""
        if not text:
            return WHOLE_FORMAT
        if self.model.token_count(text) > self.config.whole_file_max_tokens:
            return DIFF_FORMAT
        return WHOLE_FORMAT

    def prewarm(self):
        """Load both models into VRAM in the background so the first task
        does not pay for a cold start."""
        if not self.config.prewarm or self.prewarm_thread:
            return

        def ping():
            messages = [{"role": "user", "content": "ready?"}]
            for model in (self.model, self.parent.main_model):
                if model is None:
                    continue
                try:
                    model.simple_send_with_retries(messages)
                except Exception:
                    pass  # a failed warm-up is not a failed run

        self.prewarm_thread = threading.Thread(target=ping, daemon=True)
        self.prewarm_thread.start()

    # ---------------------------------------------------------------- edits

    def reset(self, coder, abs_fnames, read_only_fnames=()):
        """Wipe the worker's context without rebuilding it."""
        coder.done_messages = []
        coder.cur_messages = []
        coder.abs_fnames = set(abs_fnames)
        coder.abs_read_only_fnames = set(read_only_fnames)
        coder.aider_edited_files = set()
        coder.reflected_message = None
        coder.partial_response_content = ""
        coder.partial_response_function_call = dict()
        coder.num_reflections = 0
        coder.num_exhausted_context_windows = 0
        coder.lint_outcome = None
        coder.test_outcome = None
        coder.shell_commands = []
        coder.summarizer_thread = None
        coder.summarized_done_messages = []
        coder.summarizing_messages = None

    def edit(self, abs_fname, brief_text, edit_format=None):
        """Apply one brief to one file with an empty worker context."""
        edit_format = edit_format or self.pick_format(abs_fname)
        coder = self.coder_for(edit_format)
        self.reset(coder, [abs_fname])

        start = time.time()
        try:
            with self.io.pipeline_auto_confirm():
                coder.run(with_message=brief_text, preproc=False)
        except Exception as err:
            return EditResult(False, error=str(err), seconds=time.time() - start)
        seconds = time.time() - start

        self.calls += 1
        self._log(coder, "edit", seconds)

        edited = set(coder.aider_edited_files or ())
        if coder.num_exhausted_context_windows:
            return EditResult(
                False,
                edited,
                coder.partial_response_content,
                error="The worker ran out of context. Split the task or shrink the brief.",
                seconds=seconds,
            )
        if not edited:
            return EditResult(
                False,
                edited,
                coder.partial_response_content,
                error="The worker did not produce a usable edit.",
                seconds=seconds,
            )
        return EditResult(True, edited, coder.partial_response_content, seconds=seconds)

    # ------------------------------------------------------------ text jobs

    def ask(self, system, user, label="ask"):
        """One-shot text job (digest, diff summary). No coder, no history."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if not self.model.use_system_prompt:
            messages = [{"role": "user", "content": f"{system}\n\n{user}"}]

        start = time.time()
        try:
            reply = self.model.simple_send_with_retries(messages)
        except Exception as err:
            self.io.tool_warning(f"Worker {label} failed: {err}")
            return ""
        seconds = time.time() - start
        self.calls += 1
        if self.ledger:
            self.ledger.log(
                label.upper(),
                role="worker",
                tokens_in=self.model.token_count(messages),
                tokens_out=self.model.token_count(reply or ""),
                seconds=round(seconds, 1),
            )
        return (reply or "").strip()

    def digest(self, text, label):
        return self.ask(DIGEST_SYSTEM, f"# {label}\n\n{text}", label="digest")

    def summarise_diff(self, diff_text):
        return self.ask(SUMMARISE_SYSTEM, diff_text, label="summarise")

    # ------------------------------------------------------------- internal

    def _log(self, coder, step, seconds):
        if not self.ledger:
            return
        self.ledger.log(
            step.upper(),
            role="worker",
            tokens_in=coder.message_tokens_sent,
            tokens_out=coder.message_tokens_received,
            seconds=round(seconds, 1),
        )

    def budget_exhausted(self):
        return self.config.max_worker_calls and self.calls >= self.config.max_worker_calls

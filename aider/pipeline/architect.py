import time

from . import parsing


class StepResult:
    def __init__(self, name, text="", directives=None, tokens_in=0, seconds=0.0, error=""):
        self.name = name
        self.text = text
        self.directives = directives
        self.tokens_in = tokens_in
        self.seconds = seconds
        self.error = error

    @property
    def ok(self):
        return not self.error

    @property
    def body(self):
        return self.directives.body if self.directives else self.text

    @property
    def questions(self):
        return self.directives.questions if self.directives else []

    @property
    def verdict(self):
        return self.directives.verdict if self.directives else None


class ArchitectClient:
    """Calls the architect model once per step, with no chat history.

    Every prompt is rebuilt from the ledger, the working memory and the
    inputs for the current step. Facts fetched for a step are not carried
    into the next one unless the architect asked to remember them, which is
    what keeps its context flat over a long run.
    """

    def __init__(self, model, io, config, ledger, prompts, knowledge=None, repo_map_fn=None):
        self.model = model
        self.io = io
        self.config = config
        self.ledger = ledger
        self.prompts = prompts
        self.knowledge = knowledge
        self.repo_map_fn = repo_map_fn
        self.last_prompt = None
        self.verbose = False

    # -------------------------------------------------------------- prompts

    def token_count(self, text):
        return self.model.token_count(text)

    def system_prompt(self, language="English"):
        text = self.prompts.main_system.format(
            working_memory_tokens=self.config.working_memory_tokens,
            language=language,
        )
        if self.model.system_prompt_prefix:
            text = self.model.system_prompt_prefix + "\n" + text
        return text

    def build_messages(self, instruction, inputs=None, task=None, facts=None, omitted=0):
        """Assemble one architect prompt, stable sections first.

        Ordering matters for local servers: an unchanged prefix lets
        llama.cpp/Ollama reuse the KV cache instead of re-reading the prompt.
        """
        sections = []

        if self.repo_map_fn:
            repo_map = self.repo_map_fn(task)
            if repo_map:
                sections.append(repo_map.strip())

        ledger_view = self.ledger.render_view(
            token_count=self.token_count,
            max_tokens=self.config.ledger_view_tokens,
            current_id=task.id if task else None,
        )
        sections.append(ledger_view)

        sections.append(self.prompts.memory_prefix.strip() + "\n" + self.ledger.memory.render())

        if facts:
            rendered = "\n\n".join(fact.render() for fact in facts)
            block = self.prompts.facts_prefix.strip() + "\n" + rendered
            if omitted:
                block += f"\n\n({omitted} request(s) not answered: ask more narrowly)"
            sections.append(block)

        if inputs:
            sections.append(inputs.strip())

        sections.append(instruction.strip())

        user = "\n\n".join(section for section in sections if section)
        system = self.system_prompt()

        if self.model.use_system_prompt:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        else:
            messages = [
                {"role": "user", "content": system + "\n\n" + user},
                {"role": "assistant", "content": "Ok."},
                {"role": "user", "content": instruction.strip()},
            ]
        return messages

    # ----------------------------------------------------------------- call

    def send(self, messages, step):
        start = time.time()
        try:
            reply = self.model.simple_send_with_retries(messages)
        except Exception as err:
            return None, 0, time.time() - start, str(err)
        seconds = time.time() - start
        tokens_in = self.model.token_count(messages) or 0
        if not reply:
            return None, tokens_in, seconds, "The architect returned an empty reply."
        self.ledger.log(
            step,
            role="architect",
            tokens_in=tokens_in,
            tokens_out=self.model.token_count(reply) or 0,
            seconds=round(seconds, 1),
        )
        return reply, tokens_in, seconds, ""

    def run_step(self, step, instruction, inputs=None, task=None, spinner_text=None):
        """Run one step, resolving NEED requests before taking the answer."""
        facts = []
        omitted = 0
        rounds = 0

        while True:
            messages = self.build_messages(
                instruction,
                inputs=inputs,
                task=task,
                facts=facts,
                omitted=omitted,
            )
            self.last_prompt = messages

            if self.verbose:
                self.io.tool_output(
                    f"[pipeline] {step} prompt: ~{self.model.token_count(messages)} tokens"
                )

            reply, tokens_in, seconds, error = self.send(messages, step)
            if error:
                return StepResult(step, error=error, tokens_in=tokens_in, seconds=seconds)

            directives = parsing.parse_directives(reply, self.model.reasoning_tag)
            self.apply_memory(directives, step)

            needs = [n for n in directives.needs if n]
            answerable = needs and self.knowledge and rounds < self.config.max_need_rounds
            body_is_empty = len(directives.body.strip()) < 40

            if answerable and (body_is_empty or not directives.verdict):
                self.io.tool_output(
                    "[pipeline] architect asked for: "
                    + ", ".join(n.raw or n.kind for n in needs)
                )
                new_facts, omitted = self.knowledge.resolve(needs)
                if new_facts:
                    facts = new_facts
                    rounds += 1
                    if body_is_empty:
                        continue
                    # It answered and asked; take the answer but keep the facts
                    # available if the caller retries this step.

            return StepResult(
                step,
                text=reply,
                directives=directives,
                tokens_in=tokens_in,
                seconds=seconds,
            )

    # --------------------------------------------------------------- memory

    def apply_memory(self, directives, step):
        """Apply REMEMBER / FORGET, then enforce the cap."""
        memory = self.ledger.memory
        for item_id in directives.forget:
            memory.forget(item_id)
        for text, pinned in directives.remember:
            memory.add(text, pinned=pinned, step=step)

        if not memory.over_budget():
            return

        self.compact()
        dropped = memory.evict_to_fit()
        if dropped:
            self.ledger.log("EVICT", dropped=len(dropped))
            if self.verbose:
                self.io.tool_output(
                    f"[pipeline] evicted {len(dropped)} working memory item(s): "
                    + ", ".join(dropped)
                )

    def compact(self):
        """Ask the architect which facts to keep when memory is over budget."""
        memory = self.ledger.memory
        inputs = self.prompts.memory_prefix.strip() + "\n" + memory.render()
        messages = self.build_messages(self.prompts.compact_step, inputs=inputs)
        reply, _tokens, _seconds, error = self.send(messages, "COMPACT")
        if error or not reply:
            return

        keep, drop, rewrites = parsing.parse_compact(reply)
        for item_id, text in rewrites.items():
            memory.rewrite(item_id, text)
        for item_id in drop:
            memory.forget(item_id)
        if keep:
            memory.keep_only(keep)

# Pipeline mode: architect / worker on two local GPUs

Status: draft specification. Implementation is deferred until concurrent
work on this repo is merged. Commit policy is decided: **one git commit per
accepted task**; do not squash at the end of a run.

This document specifies a new opt-in aider mode ("pipeline mode") in which a
stronger local model acts as an **architect** (plans, briefs, reviews) and a
smaller local model acts as a **worker** (edits one file at a time, writes
tests, answers narrowly-scoped questions about code). The two models run
concurrently on two GPUs (e.g. a 16 GB card and an 8 GB card) on Windows, and
the orchestration is designed so that **neither model ever needs a large
context window**.

It is written so that another model/engineer can implement it phase by phase
without further design discussion. Sections 1 to 3 are assessment and design
rationale; sections 4 onwards are the normative spec.

---

## 0. Executive summary

- Aider already has an architect/editor split (`--architect`, `--editor-model`).
  What it lacks: per-task worker isolation, a verification step (the architect
  never sees the diff), a test loop that involves the architect, and any
  mechanism to keep the architect's context small. Today the architect is sent
  the *full contents of every file in the chat* on every turn.
- The requested hardware split is the right way round (big model = architect,
  small model = worker), but the worker is the quality bottleneck, so the
  design leans hard on making worker tasks tiny, single-file and
  machine-verifiable, and on using the `whole` edit format for the worker.
- The architect's context must be bounded by construction, not by hoping
  summaries are short. We make the architect **stateless per call**: every
  architect call is built from a persisted **ledger** (plan + task statuses),
  a token-capped **working memory** of facts the architect has explicitly
  chosen to keep, the repo map, and only the inputs for the current step.
  Everything else is discarded after each step. This is the "read it, use it,
  forget it" behaviour a human would have.
- Repo understanding is **pull-based and tiered**: repo map (free, structural)
  -> static lookups via tree-sitter/grep (free, exact) -> cached per-symbol
  digests produced by the worker (cheap) -> raw source slices (last resort).
  A blanket "summarise every function up front" pass is offered as an optional
  bootstrap command but is not the default; it is slow and its output would be
  mostly irrelevant to any given task.
- Delivery is in five phases. Phase 0 is configuration only (two servers, two
  models, existing `--architect`) and produces a measured baseline. Phase 1
  is the minimal orchestration loop. Phases 2 to 4 add the knowledge tiers,
  the test loop and robustness. Implementation waits until concurrent work
  on this repo is merged. Git policy: one commit per accepted task, never
  squashed.

---

## 1. Assessment of the proposal

### 1.1 What aider does today

`aider/coders/architect_coder.py` (`ArchitectCoder`, edit format `architect`):

1. The architect model receives the system prompt, repo map, **full contents
   of all files added to the chat**, and chat history. It replies with prose
   describing the changes.
2. `reply_completed()` creates an editor `Coder` via `Coder.create(from_coder=self, ...)`.
   That copies `abs_fnames` (all chat files) and `abs_read_only_fnames` into
   the editor, sets `map_tokens=0`, then clears `cur_messages`/`done_messages`
   so the editor starts with an empty history. The editor gets the *entire*
   architect reply as its instruction and edits any of the files.
3. The architect's history gets a synthetic "I made those changes to the
   files." message. The architect **never sees what was actually changed**.
   Lint/test auto-fix loops (`send_message` in `base_coder.py`) run inside the
   editor's `run_one` reflection loop, not through the architect.

So the per-turn worker context wipe already exists, but the architect's own
context grows with every file added, and there is no verification, no
task decomposition, and no test hand-off.

### 1.2 Hardware and model fit

The user's suggested models, with realistic VRAM budgets. Sizes are for GGUF
quantisations served by llama.cpp/Ollama; verify against the actual files at
implementation time since quant releases vary by a few hundred MB.

| Role | Model | Weights (approx) | KV cache / token (f16) | Fits? |
|---|---|---|---|---|
| Architect | Qwen3-Coder-30B-A3B-Instruct, Q4_K_M | ~18.6 GB | ~96 KB (48 layers, 4 KV heads, d=128) | **No, not fully on a 16 GB card.** Use Q3_K_M / IQ3 (~13 to 15 GB) fully on GPU with q8_0 KV and 24 to 32k context, or Q4 with expert tensors offloaded to CPU (`--n-cpu-moe` / `-ot "exps=CPU"`); MoE with 3.3B active params stays usable at CPU-offloaded expert speeds. |
| Architect (alt) | gpt-oss-20b, MXFP4 | ~12.1 GB | small (24 layers, 8 KV heads, d=64, half the layers sliding-window) | Yes, with 32k+ context to spare. |
| Architect (alt) | Qwen3-30B-A3B-Thinking / Instruct (2507) | as Qwen3-Coder | as above | Same constraints as Qwen3-Coder. |
| Worker | "Qwen3 2.5 coder" does not exist as a product. Assumed: **Qwen2.5-Coder-7B-Instruct**, Q4_K_M | ~4.7 GB | ~56 KB | Yes, 32k context fits in 8 GB with q8_0 KV. |
| Worker (alt) | Qwen2.5-Coder-14B-Instruct Q4_K_M | ~9 GB | | No (8 GB). |
| Worker (alt) | Qwen3-8B / Qwen3-4B (general, thinking) | ~5 GB / ~2.5 GB | | Yes, but they are not code-specialised; benchmark before choosing. |

Implication: even with no orchestration change, the architect card is limited
to roughly a 24 to 32k window with the 30B MoE, and the worker to about 32k.
Because local prompt processing is slow (prefill is often 500 to 2000 tok/s on
these cards), **small contexts are also a speed requirement, not just a VRAM
one**. Pipeline mode should target roughly 12k tokens per architect call and
8k per worker call as design budgets (section 5.4), leaving headroom.

### 1.3 Which model should be the architect?

Benchmark evidence available in this repo (`aider/website/_data/*.yml`):

- Qwen2.5-Coder-32B-Instruct, polyglot: 16.4% (`whole`), 8.0% (`diff`);
  `whole` well-formed 99.6% vs `diff` 71.6%. On the older Exercism python
  benchmark it scored 71.4%. The 7B is materially weaker than the 32B.
- Qwen3-32B: 40 to 46% polyglot depending on settings/format.
- gpt-oss-120b (high): 41.8% polyglot, 79.1% well-formed. No repo data for
  gpt-oss-20b or Qwen3-Coder-30B-A3B.

Recommendation and challenge:

- **Qwen3-Coder-30B-A3B as architect (default).** Code-trained, non-thinking
  (fast, predictable output length), 256k native context (we will not use it,
  but it degrades gracefully), and it produces edit instructions in a format
  small models follow. Weakness: it is an instruct model without explicit
  reasoning, so multi-step planning quality is the risk. Mitigation is
  structural: the orchestrator asks it small, single-purpose questions (plan,
  brief, review, triage) rather than "design and describe everything".
- **gpt-oss-20b as architect (alternative, worth benchmarking).** Better at
  deliberate reasoning at `reasoning_effort=high`, fits comfortably with a
  large context. Risks: long reasoning chains cost wall-clock time on a 16 GB
  card; the harmony chat format needs `--jinja` in llama-server and aider's
  reasoning-content stripping must be verified; edit-format adherence is
  historically weak but irrelevant in a pure planning role. It is a credible
  architect candidate, probably a poor worker.
- **Do not put the small model in the architect seat.** Planning and review
  require the strongest model. Editing a single function against an explicit
  brief is the easiest task in the system and is the right job for the 7B.
- The pipeline must make the architect and worker independently configurable
  (`--pipeline-architect-model`, `--pipeline-worker-model`) so the user can
  A/B them on their own repo. Phase 0 includes a tiny benchmark harness for
  exactly this.

### 1.4 Point-by-point on the proposed workflow

| Proposal | Assessment | What the spec does |
|---|---|---|
| Architect designs, then feeds worker one file at a time with explicit input/output/behaviour instructions. | Correct, and it matches the worker's capability envelope. Challenge: many changes are cross-file (signature change + callers). One-file-at-a-time works only if the architect **orders** tasks (interfaces first) and each brief carries the **context the worker cannot see** (new signatures in other files, as read-only snippets). | Task = one editable file. Brief schema includes `context_snippets` (resolved from other files by the orchestrator, read-only) and tasks are ordered by dependency (section 5.2, 8.2). |
| Worker context wipes after each task. | Already how `ArchitectCoder` works per turn; formalise it. | A fresh worker `Coder` per task via `Coder.create(...)` with `done_messages=[]`, `cur_messages=[]`, `fnames=[target]`, `map_tokens=0` (section 6.3). |
| Architect checks the changes were what was intended before moving on. | Essential and currently missing. Challenge: the architect must not re-read the file (context). A diff is small and precise. | Review step: architect receives the brief + `git diff` of the target file + lint result, returns ACCEPT / RETRY / REPLAN (section 5.3, step R). Diff is size-capped; oversize diffs are summarised by the worker first. |
| Architect gets the worker doing unit tests, then next function. | Good. Challenge: test output can be huge and noisy; a 7B is bad at writing tests without a spec. Also decide ordering (tests-after vs TDD). | Test brief from the architect with named cases; worker writes the test file; orchestrator runs `--test-cmd`; output is trimmed (summary lines + first failure) before triage by the architect. `--pipeline-tdd` optionally writes tests first (section 5.3, step T). |
| Worker reads each function and summarises it one at a time at the start so the architect can "see" the repo. | **Challenge.** (a) Aider's repo map already gives a ranked structural view (signatures) for free. (b) Summarising every function in a mid-size repo is thousands of worker calls (~20+ min for a few hundred functions on a 7B) and most of it is irrelevant to any given request. (c) 7B summaries can be subtly wrong; a wrong summary is worse than a signature. | Tiered, pull-based knowledge (section 5.5): repo map -> exact static lookups (definitions, references, call sites via tree-sitter/grep) -> **on-demand** per-symbol digests written by the worker and cached on disk keyed by content hash -> raw source slices. An optional `/pipeline digest <paths>` bootstrap exists for users who want it. |
| (Follow-up) Even summarised, the collated info may overflow the architect. A human reads things and keeps only what matters. | Agreed; this is the central design constraint. Summaries do not bound context, **budgets and eviction do**. | Stateless architect calls built from a ledger + a token-capped working memory. Retrieved facts live only for the step that requested them unless the architect explicitly writes a `REMEMBER:` line. Working memory has a hard cap; on overflow the architect is asked to compact it, and the orchestrator evicts unpinned, oldest facts if it does not (section 5.4). |

### 1.5 Things the user did not ask about but that will bite

- **Edit format for the worker.** Small models are far more reliable with
  `whole` than search/replace `diff` (99.6% vs 71.6% well-formed for the 32B
  above; worse for 7B). Default the worker to `editor-whole` and let the
  orchestrator switch to `editor-diff` only when the target file exceeds a
  token threshold (whole-file output is slow on an 8 GB card: a 400-line file
  is ~4k output tokens, roughly 1 to 2 minutes at 7B speeds).
- **Two endpoints, two ports.** Aider normally takes one `OPENAI_API_BASE`.
  Each model needs its own `api_base`, set via `extra_params` in
  `.aider.model.settings.yml` (passed through to litellm). Phase 0 verifies
  this works for both `openai/` and `ollama_chat/` prefixes.
- **Token counting is approximate for local models.** Aider uses litellm's
  counters, not the model's tokenizer. Budgets in this spec assume a 20%
  safety margin, and `max_input_tokens` must be declared in
  `.aider.model.metadata.json` so `check_tokens()` can warn.
- **Prompt-prefix caching matters more than usual.** Because each architect
  call re-sends the same prefix (system prompt, repo map, ledger), ordering
  the stable parts first and enabling `--cache-reuse` in llama-server makes
  repeated architect calls cheap. Working memory and step inputs go last.
- **Interactive confirmations.** Aider's `io.confirm_ask` is everywhere. The
  pipeline needs a scoped "auto-yes" for internal worker runs while still
  letting the user approve the plan and, optionally, each task.
- **Concurrency is available but not used by a sequential loop.** With both
  models resident, the architect can draft brief N+1 while the worker executes
  task N. This is a Phase 4 optimisation, not a requirement.
- **Crash recovery.** Local servers die; a 40-minute run must resume. The
  ledger is on disk and every accepted task is a git commit.

---

## 2. Goals and non-goals

Goals

1. Run architect and worker on separate GPUs/servers concurrently, configured
   in one place, working on Windows (PowerShell) as well as Linux/macOS.
2. Decompose a user request into single-file tasks, execute each with a fresh
   worker context, verify each against the brief, commit each, and run tests,
   with automatic hand-offs and bounded retries.
3. Keep every architect call under a configurable token budget by
   construction (ledger + working memory), independent of repo size or run
   length.
4. Give the architect cheap, accurate ways to learn about the repo without
   loading files: static lookups first, cached worker digests second.
5. Persist state so runs are inspectable (`/pipeline status`), editable, and
   resumable.
6. Keep the change additive and isolated from concurrent development on the
   core coders: new modules, minimal edits to `base_coder.py`.

Non-goals (for this spec)

- Multi-repo, multi-worker fan-out, or more than two models.
- Replacing the existing `architect` mode. Pipeline mode is a new edit format.
- GUI/browser support.
- Perfect JSON adherence from local models; we parse leniently and retry.
- Squashing pipeline commits. One accepted task is one commit, left in
  history as-is.

---

## 3. Design principles

1. **Bounded by construction.** No component's context may grow with run
   length or repo size. Every prompt section has a token cap and a
   truncation/eviction rule.
2. **Stateless architect, stateful ledger.** The architect has no chat
   history. Its "memory" is the ledger and a curated working memory it
   controls through explicit `REMEMBER`/`FORGET` directives.
3. **Pull, don't push.** Nothing about the repo is put in front of the
   architect unless it is in the repo map or the architect asked for it.
4. **Cheapest source of truth first.** tree-sitter and grep before an LLM;
   the worker before the architect; a diff before a file.
5. **Small, verifiable worker tasks.** One editable file, explicit
   acceptance criteria, lint + diff review + tests, automatic retry with a
   revised brief, hard retry caps.
6. **Every accepted task is exactly one commit.** The commit is created
   when REVIEW returns ACCEPT, covers only that task's target file, and is
   never squashed with other pipeline commits at DONE. Retries happen
   *before* ACCEPT, so a retried task still produces one commit. Post-commit
   test fixes become a new task with its own commit. Aider's `/undo`
   semantics apply per task. Do not implement `/pipeline squash`.
7. **Human checkpoints are configurable, not mandatory.** Approve plan only
   (default), approve every task, or fully automatic.

---

## 4. Architecture overview

```
 user request
      |
      v
 PipelineCoder (edit_format="pipeline")          aider/coders/pipeline_coder.py
   | owns: Ledger, WorkingMemory, KnowledgeService, WorkerPool
   |
   |-- Architect calls (stateless, one per step)  aider/pipeline/architect.py
   |      PLAN | BRIEF | REVIEW | TEST_BRIEF | TRIAGE | COMPACT
   |      prompt = system + repo map + ledger view + working memory
   |               + retrieved facts (this step only) + step inputs
   |
   |-- Worker sessions (fresh Coder per task)      aider/pipeline/worker.py
   |      EDIT (editor-whole / editor-diff) | WRITE_TEST | DIGEST | SUMMARISE
   |
   |-- KnowledgeService                            aider/pipeline/knowledge.py
   |      repo map | defs/refs (RepoMap tags) | grep | digest cache | source slice
   |
   |-- Ledger (YAML on disk)                       aider/pipeline/ledger.py
   |-- WorkingMemory (token-capped facts)          aider/pipeline/memory.py
   |-- Verification (git diff, lint, tests)        aider/pipeline/verify.py
   |
   v
 git commits per accepted task
```

Two model servers run underneath (section 7): e.g. `llama-server` on
`127.0.0.1:8081` bound to GPU 0 (architect) and `127.0.0.1:8082` bound to GPU 1
(worker). Aider addresses them as two `Model` objects with per-model
`api_base`.

---

## 5. Detailed design

### 5.1 The ledger

Persisted at `.aider.pipeline/ledger.yml` in the repo root (add
`.aider.pipeline/` to the user's `.gitignore`; the orchestrator offers to do
so on first run, mirroring how aider handles `.aider*`).

```yaml
version: 1
request: |
  <the user's original request, verbatim>
clarifications:            # any user answers gathered during PLAN
  - "Use the existing Settings dataclass; do not add a config file."
plan_summary: |            # <= plan_summary_tokens (default 400)
  Add rate limiting to the API client. Introduce RateLimiter in
  net/ratelimit.py, wire into ApiClient.request, cover with unit tests.
tasks:
  - id: T1
    title: Create RateLimiter class
    file: net/ratelimit.py
    kind: edit                  # edit | test | new_file
    symbols: [RateLimiter]      # editable symbols (informational for whole-file; enforced for diff)
    depends_on: []
    status: accepted            # pending | briefed | editing | review | testing | accepted | failed | skipped
    attempts: 1
    commit: 3f9a1c2
    brief_path: .aider.pipeline/briefs/T1.md
    notes: "Token bucket, monotonic clock, thread-safe."
  - id: T2
    title: Wire RateLimiter into ApiClient.request
    file: net/client.py
    kind: edit
    symbols: [ApiClient.request, ApiClient.__init__]
    depends_on: [T1]
    status: pending
    attempts: 0
  - id: T3
    title: Unit tests for RateLimiter
    file: tests/test_ratelimit.py
    kind: test
    depends_on: [T1]
    status: pending
working_memory:              # see 5.4; each item has a token cost
  - id: M1
    pinned: true
    text: "ApiClient.request(self, method, path, **kw) -> Response; all HTTP goes through it."
    source: "static:defs net/client.py"
    step: PLAN
  - id: M2
    pinned: false
    text: "tests use pytest + responses lib; fixtures in tests/conftest.py"
    source: "digest tests/conftest.py"
    step: PLAN
history:                     # append-only, never sent to the architect in full
  - {ts: ..., step: PLAN, tokens_in: 9800, tokens_out: 900}
  - {ts: ..., step: BRIEF, task: T1, tokens_in: 6100, tokens_out: 700}
  - {ts: ..., step: REVIEW, task: T1, verdict: ACCEPT}
```

The **ledger view** sent to the architect is a rendered subset: `request`,
`clarifications`, `plan_summary`, and for each task only
`id / title / file / status / depends_on / notes`. Briefs and history are not
included. The view is capped (`ledger_view_tokens`, default 1500); if the task
list is longer than the cap, accepted tasks collapse to one line each and
then to a count ("7 tasks accepted: T1..T7").

### 5.2 Task decomposition rules (enforced by the orchestrator, requested of the architect in the PLAN prompt)

- Exactly one editable file per task. New files are allowed (`kind: new_file`).
- Tasks are topologically ordered by `depends_on`. Interface/signature changes
  come before their callers. The orchestrator validates the DAG and rejects
  cycles with a reflection message.
- A task should be completable by a small model given only the target file
  and a handful of read-only snippets. If the architect cannot describe a task
  in one screen of instructions, it should split it.
- Test tasks are separate tasks (`kind: test`) that depend on the code tasks
  they cover. With `--pipeline-tdd`, the orchestrator schedules test tasks
  before their dependencies and expects them to fail first.
- Maximum tasks per plan: `max_tasks` (default 25). Beyond that the architect
  is told to produce a coarser plan or the user is asked to narrow the request.

### 5.3 The state machine

```
IDLE --user request--> PLAN --> (user approves plan?) --> SCHEDULE
SCHEDULE: pick next task whose depends_on are all accepted; none left -> DONE
  |
  v
BRIEF(task)   architect writes the brief (may emit NEED requests first; see 5.5)
  |
  v
EDIT(task)    fresh worker applies the brief to the single file
  |
  v
VERIFY(task)  orchestrator: syntax/lint the file, compute git diff (capped)
  |
  v
REVIEW(task)  architect: brief + diff + lint -> ACCEPT | RETRY(revised brief) | REPLAN
  |             RETRY: attempts < max_attempts (default 2) -> EDIT with revised brief
  |             else FAILED -> ask user (skip / edit manually / abort)
  v
COMMIT(task)  git commit of the target file, message from brief title
  |
  v
TEST(task)?   if task.kind == test or a test_cmd is configured and task touched
  |           code covered by existing tests: run test_cmd, trim output
  |           failures -> TRIAGE(architect): FIX_CODE | FIX_TEST | ACCEPT_KNOWN | ESCALATE
  |             FIX_CODE / FIX_TEST insert a *new* task (new file-scoped brief, own commit);
  |             they never amend or reopen the already-committed task.
  v
SCHEDULE
```

Step definitions (inputs -> outputs). All architect steps share the common
prefix described in 5.4.

**PLAN**
- Inputs: request, repo map (architect-sized, see 5.5), any facts retrieved
  during a `NEED` round.
- Output: `plan_summary` and the task list as a fenced YAML block matching the
  ledger task schema, plus optional `QUESTION:` lines for the user and
  `REMEMBER:` lines (5.4). The architect may instead reply only with `NEED:`
  lines; the orchestrator answers them and re-asks PLAN (max
  `max_need_rounds`, default 3).
- Validation: YAML parses, files exist or are marked `new_file`, DAG is
  acyclic, task count within cap. Failures are reflected back once with the
  error text, then the user is asked.
- User checkpoint: plan is shown; user can accept, edit the ledger file and
  reload, or abort (`--pipeline-approve plan|task|never`).

**BRIEF(task)**
- Inputs: the task entry, the target file's **outline** (tree-sitter defs
  with line numbers, not the source), and any `NEED` results. The architect
  may request a source slice of specific symbols in the target file if the
  outline is insufficient (`NEED: source net/client.py::ApiClient.request`).
- Output: a brief in the fixed format (section 8.2) saved to
  `.aider.pipeline/briefs/<id>.md`. It must include acceptance criteria
  written as checkable statements and a list of `context_snippets`
  (`file::symbol` or `file:L10-L40`) which the orchestrator resolves to
  read-only text for the worker. Snippet total is capped
  (`worker_snippet_tokens`, default 2000); oversize requests are trimmed
  from the end and the architect is warned in REVIEW.

**EDIT(task)**
- A fresh worker coder (6.3) with the target file editable, snippets read-only,
  and the brief as the sole user message. Edit format: `editor-whole` if the
  file is under `whole_file_max_tokens` (default 3000), else `editor-diff`.
- Worker output is applied by the worker coder's normal `apply_updates()`.
  `auto_commits`, `auto_lint`, `auto_test` are off on the worker; the
  orchestrator owns those.
- If the worker edits a file other than the target (possible with `diff`
  formats via file mentions), the orchestrator reverts those hunks with
  `git checkout -- <file>` and notes it in the REVIEW input.

**VERIFY(task)**
- Runs aider's `Linter` on the target file (respects `--lint-cmd`). Computes
  `git diff -- <file>` (unified, 3 lines context). If the diff exceeds
  `review_diff_tokens` (default 2500), a worker `SUMMARISE` call condenses it
  to a per-hunk description and the raw diff is truncated after the cap.

**REVIEW(task)**
- Inputs: brief, diff (or summary), lint output (trimmed to
  `lint_output_tokens`, default 800), attempt number.
- Output: exactly one verdict line `VERDICT: ACCEPT|RETRY|REPLAN` followed by
  rationale; for RETRY a full revised brief; for REPLAN a new task list
  fragment (tasks to insert/replace). `REMEMBER:` lines allowed (e.g. "T1
  exposes RateLimiter.acquire(timeout=None)").
- The review prompt tells the architect explicitly: "You are checking the
  diff against the brief and acceptance criteria. Do not redesign. Prefer
  ACCEPT with a note over RETRY for cosmetic issues."

**COMMIT(task)**
- `repo.commit(fnames=[file], message=f"pipeline {id}: {title}")` via aider's
  `GitRepo`, recorded in `task.commit`. Uses the existing aider commit
  attribution settings. This is the only commit for that task: do not amend
  it later, do not squash it into neighbouring pipeline commits, and do not
  fold later test-fix work into it. If tests later require more code
  changes, schedule a new task.

**TEST(task)**
- Runs `test_cmd` through `commands.cmd_test`-equivalent plumbing but captures
  output instead of appending it to a chat. Output is trimmed by
  `verify.trim_test_output()`: keep the final summary block and the first
  failing test's traceback, cap at `test_output_tokens` (default 1500).
- TRIAGE(architect) inputs: task list view, trimmed output. Output:
  `VERDICT: FIX_CODE|FIX_TEST|ACCEPT_KNOWN|ESCALATE`. For the FIX
  verdicts, the architect also emits a new task (same schema as PLAN: one
  file, depends_on the just-committed task) plus its brief. The orchestrator
  inserts that task into the ledger and schedules it; it becomes its own
  commit when accepted. Do not reopen, amend, or squash the original task.
  Inserted triage tasks count against `max_test_rounds` (default 3) for the
  original task so a failing test cannot spawn unbounded follow-up work.

**COMPACT** (architect, triggered by the orchestrator, see 5.4)
- Inputs: current working memory items with ids and token costs, and the
  remaining task list.
- Output: a replacement list of `KEEP <id>` / `DROP <id>` / `REWRITE <id>:
  <text>` lines. The orchestrator applies it and re-checks the cap.

### 5.4 Working memory and the architect's per-call context

This is the answer to "the architect's context is limited too".

**Composition of every architect call** (in this order, stable prefix first
for KV-cache reuse), with default caps for a 32k window (all configurable;
defaults sum to ~12.5k plus output):

| Section | Content | Cap (tokens) |
|---|---|---|
| System prompt | role + step protocol + output format | ~1200 (fixed) |
| Repo map | aider `RepoMap` ranked for the current task's file/symbols | `architect_map_tokens` = 2000 |
| Ledger view | request, clarifications, plan summary, compact task list | 1500 |
| Working memory | pinned first, then newest first | `working_memory_tokens` = 2000 |
| Retrieved facts | answers to this step's `NEED` requests only | `facts_tokens` = 3000 |
| Step inputs | brief / diff / lint / test output for this step | step-specific, see 5.3 |
| Step question | the instruction for this step | ~300 |

**Retention rules**

- Retrieved facts are **dropped at the end of the step**. To keep anything,
  the architect must write `REMEMBER: <one line, <= 60 tokens>` in its
  reply. The orchestrator turns each into a working-memory item recording
  the source and step.
- `REMEMBER PINNED: <text>` marks an item as not evictable by the orchestrator
  (only the architect can drop it via COMPACT or `FORGET <id>`).
- Each working-memory item is shown with its id and token cost so the
  architect can manage it.
- When adding items would exceed `working_memory_tokens`, the orchestrator
  runs COMPACT once. If still over the cap, it evicts unpinned items oldest
  first and logs the eviction in `history`. If pinned items alone exceed the
  cap, the architect is told in the next step that it must unpin.
- Task `notes` (one line per task, written by the architect at REVIEW) live
  in the ledger, not working memory, so accepted-task knowledge survives
  eviction in a compact form.

**Why this replicates "read and ignore"**: the architect sees potentially
large facts (a digest, a source slice, a test failure) only while answering
the question that needed them, and must deliberately distil what is worth
carrying forward. The orchestrator makes forgetting the default and
remembering explicit and cheap.

**Failure mode to guard against**: an architect that REMEMBERs everything.
The system prompt states the cap and that the list is shown to it every
step; the COMPACT step provides pressure; the orchestrator's eviction is
the backstop. Telemetry (`/pipeline status`) shows memory utilisation so the
user can tune caps or prompts.

### 5.5 Knowledge service: how the architect learns about the repo

Tiered lookups, exposed to the architect as `NEED:` requests it can put in
any reply. The orchestrator resolves them and re-asks the same step with the
results in the "retrieved facts" section. Each result is capped and labelled
with its tier and cost.

| Tier | Request syntax | Resolution | Cost |
|---|---|---|---|
| 0 | (always present) | `RepoMap.get_repo_map()` with `mentioned_fnames`/`mentioned_idents` biased toward the current task's file and symbols | none beyond map tokens |
| 1 | `NEED: outline <file>` | tree-sitter definitions with line numbers (`RepoMap.get_tags(kind=="def")`) | none |
| 1 | `NEED: refs <symbol>` | tags of kind `ref` for the identifier, grouped by file with line numbers; falls back to a word-boundary grep over tracked files | none |
| 1 | `NEED: grep <regex>` | `rg`/Python regex over tracked files, max `grep_hits` (default 40) hits with 1 line of context | none |
| 2 | `NEED: digest <file>` or `NEED: digest <file>::<symbol>` | cached digest if `sha256(content slice)` matches; otherwise a fresh worker `DIGEST` call on that slice only (system prompt in 8.4), stored under `.aider.pipeline/digests/<hash>.md` | one worker call per uncached symbol |
| 3 | `NEED: source <file>::<symbol>` or `NEED: source <file>:L10-L60` | raw source slice, capped at `source_slice_tokens` (default 1200) | none, but expensive in architect context |

Rules:

- Resolution order for a bare `NEED: about <symbol>` (convenience form):
  tier 1 defs/refs first; if the symbol is a definition, add its tier 2
  digest. Never auto-escalate to tier 3.
- All facts for one round must fit `facts_tokens`; the orchestrator fills in
  request order and marks anything omitted ("3 more results omitted, ask
  more narrowly").
- Digest prompts ask for: purpose (1 line), inputs/outputs (1 to 2 lines),
  side effects/dependencies (1 line), gotchas (0 to 1 line). Hard cap 120
  tokens per symbol, 300 per file. The digest cache is content-addressed, so
  editing a file invalidates only its changed symbols.
- **Optional bootstrap**: `/pipeline digest <paths...>` pre-computes tier 2
  digests for the given files (or, with no args, the top N files by repo-map
  rank for the current request). It reports estimated worker calls and time
  before running and is interruptible. This is the user's "summarise
  everything at the start" idea, made opt-in and scoped.

### 5.6 Worker sessions

- One `Coder` per task, created with `Coder.create(main_model=worker_model,
  edit_format=..., io=io, from_coder=None, ...)`, **not** `from_coder=self`,
  to avoid inheriting the pipeline coder's file set and history. Pass the
  shared `repo`, `commands`-free operation, `fnames=[target_abs]`,
  `read_only_fnames=[]` (snippets are inlined into the brief as fenced blocks
  labelled read-only, which small models handle better than a second files
  section), `map_tokens=0`, `auto_commits=False`, `auto_lint=False`,
  `auto_test=False`, `suggest_shell_commands=False`, `cache_prompts=False`,
  `stream=False` unless `--pipeline-stream-worker`.
- Worker runs are wrapped in an "auto-yes" context so `confirm_ask` calls
  inside `apply_updates` (e.g. creating a new file) do not block; the
  orchestrator remains responsible for user checkpoints.
- Per-call output cap: `max_reflections=1` on the worker; if the worker fails
  to produce a well-formed edit twice, the attempt is marked failed and goes
  to REVIEW as such (the architect may simplify the brief).
- DIGEST and SUMMARISE worker calls use an `AskCoder`-style read-only coder
  (edit format `ask`) with the slice inlined, and no files.

### 5.7 Approval modes and user interaction

- `--pipeline-approve plan` (default): show the plan once; every subsequent
  step is automatic until DONE, a FAILED task, or an ESCALATE.
- `--pipeline-approve task`: additionally pause before each EDIT showing the
  brief, and after each REVIEW showing the verdict.
- `--pipeline-approve never`: fully automatic (intended for scripted runs).
- At any pause, the user can `/pipeline edit` (opens the ledger in
  `$EDITOR`, reloads), `/pipeline skip <id>`, `/pipeline retry <id>`,
  `/pipeline abort`. Architect `QUESTION:` lines pause with the question
  regardless of mode unless `never`, in which case the orchestrator answers
  "Use your best judgement and note the assumption" and records it in
  `clarifications`.

### 5.8 Budgets, limits, telemetry

All defaults live in one dataclass (`PipelineConfig`) and are overridable by
CLI flags (`--pipeline-*`) and `.aider.conf.yml`:

```
architect_map_tokens=2000  ledger_view_tokens=1500  working_memory_tokens=2000
facts_tokens=3000          review_diff_tokens=2500  lint_output_tokens=800
test_output_tokens=1500    worker_snippet_tokens=2000  whole_file_max_tokens=3000
source_slice_tokens=1200   grep_hits=40             max_tasks=25
max_attempts=2             max_test_rounds=3        max_need_rounds=3
max_worker_calls=200       max_wall_seconds=0 (unlimited)
```

Every architect/worker call appends `{step, task, tokens_in, tokens_out,
seconds}` to `history`. `/pipeline status` prints task table, memory
utilisation, calls made, and estimated remaining work.

---

## 6. Code changes

### 6.1 New files

```
aider/coders/pipeline_coder.py        PipelineCoder(Coder), edit_format="pipeline"
aider/coders/pipeline_prompts.py      architect step prompts, worker digest/summarise prompts
aider/pipeline/__init__.py
aider/pipeline/config.py              PipelineConfig dataclass + CLI/yaml merge
aider/pipeline/ledger.py              Ledger load/save/validate/render_view
aider/pipeline/memory.py              WorkingMemory (items, caps, eviction, parse REMEMBER/FORGET)
aider/pipeline/knowledge.py           KnowledgeService (tiers 0-3, digest cache)
aider/pipeline/architect.py           ArchitectClient: build prompt per step, call model, parse outputs
aider/pipeline/worker.py              WorkerFactory: fresh coders for EDIT/WRITE_TEST/DIGEST/SUMMARISE
aider/pipeline/verify.py              lint, diff capture, test run, trimming helpers
aider/pipeline/parsing.py             lenient fenced-YAML / directive-line parsers
aider/pipeline/briefs.py              brief schema, render, snippet resolution
tests/basic/test_pipeline_ledger.py
tests/basic/test_pipeline_memory.py
tests/basic/test_pipeline_parsing.py
tests/basic/test_pipeline_knowledge.py
tests/basic/test_pipeline_coder.py    end-to-end with mocked models
scripts/pipeline/start-architect.ps1  llama-server on GPU 0
scripts/pipeline/start-worker.ps1     llama-server on GPU 1
scripts/pipeline/start-both.ps1       launches both in separate windows, waits for /health
scripts/pipeline/start-both.sh        Linux/macOS equivalent
scripts/pipeline/bench.py             Phase 0 mini-benchmark (section 9.1)
aider/website/docs/usage/pipeline.md  user docs
aider/website/docs/llms/dual-gpu-local.md  Windows dual-GPU setup guide
```

### 6.2 Edits to existing files (keep minimal to avoid conflicts with concurrent work)

- `aider/coders/__init__.py`: import and register `PipelineCoder`.
- `aider/args.py`: add `--pipeline` (sets `edit_format="pipeline"`),
  `--pipeline-architect-model`, `--pipeline-worker-model`,
  `--pipeline-worker-edit-format`, `--pipeline-approve`, `--pipeline-tdd`,
  `--pipeline-ledger` (path), and `--pipeline-<budget>` flags generated from
  `PipelineConfig` fields.
- `aider/main.py`: when `--pipeline`, construct the worker `Model` and pass
  both to `Coder.create` via kwargs (`pipeline_worker_model=...`). Reuse
  `sanity_check_model` on both.
- `aider/commands.py`: `cmd_pipeline` dispatching `status|edit|skip|retry|abort|digest|resume|plan`.
  `/pipeline plan <request>` starts a run without leaving the normal chat
  loop; a bare message while in pipeline mode does the same.
- `aider/coders/base_coder.py`: **no changes required** for Phase 1 to 3.
  `PipelineCoder` overrides `run_one`, `get_chat_files_messages` (returns
  nothing: the architect never sees file contents), and
  `get_announcements`. If a hook is later needed (e.g. exposing a scoped
  auto-yes), add it as a small, additive method.
- `aider/io.py`: add a context manager `io.auto_yes()` that temporarily
  sets `self.yes = True` and restores it; used around worker runs. (Tiny,
  additive.)

### 6.3 Key implementation notes for the implementer

- `Coder.create(from_coder=self)` copies `abs_fnames`, history and
  `original_kwargs`; do not use it for workers. Build worker kwargs
  explicitly. `Coder.__init__` requires `main_model, io` and accepts
  `repo`, `fnames`, `read_only_fnames`, `map_tokens`, `auto_commits`,
  `auto_lint`, `auto_test`, `lint_cmds`, `test_cmd`, `stream`,
  `suggest_shell_commands`, `cache_prompts`, etc. (see signature at
  `base_coder.py:299`). Pass `total_cost` back and forth only for display.
- The architect step call should not go through `Coder.run`; use
  `Model.simple_send_with_retries(messages)` (non-streaming, returns the
  content string; `models.py`) or `Model.send_completion(messages,
  functions=None, stream=False)` when usage/token counts are needed, so no
  chat state accumulates. Strip reasoning with the same logic as
  `Coder.remove_reasoning_content` for models with `reasoning_tag` set
  (factor that into a shared helper rather than duplicating it).
- Repo map for the architect: instantiate one `RepoMap` in `PipelineCoder`
  with `map_tokens=config.architect_map_tokens`, `refresh="files"`, and call
  `get_repo_map(chat_files={task_file}, other_files=rest,
  mentioned_fnames=..., mentioned_idents=task.symbols)` per step.
- Tier 1 lookups reuse `RepoMap.get_tags(fname, rel_fname)` (cached by
  mtime in `TAGS_CACHE`); `Tag.kind in {"def","ref"}` and `Tag.line` give
  what is needed. For `outline`, take `def` tags, sort by line, and include
  the source line text for each (a one-line signature).
- Symbol-to-line-range resolution for `source`/`digest <file>::<symbol>`:
  find the `def` tag by name, then take lines from that def to the next def
  at the same or shallower indentation (Python) or to the matching brace via
  tree-sitter node `end_point` when the language grammar is available.
  Fall back to a fixed window (`+/- 40 lines`) with a "range approximate"
  label.
- Diff capture: `repo.repo.git.diff("--", rel_fname)` for unstaged changes
  after the worker writes files (aider writes files directly; nothing is
  staged). Commit with `repo.commit(fnames=[abs_fname], message=...,
  aider_edits=True)`.
- Windows: `run_cmd` already branches to `subprocess` on Windows; test
  commands should be given as shell strings (`pytest -q`). Use `pathlib` and
  `repo.get_rel_fname`; do not assume `/` separators in ledger paths (store
  POSIX-style, convert on use).
- Lenient parsing: extract the first fenced block whose info string is
  `yaml` (or the largest fenced block if none is labelled); strip `<think>`
  blocks and trailing prose; `yaml.safe_load`; on failure retry once with
  the parser error appended as a reflection. Directive lines
  (`VERDICT:`, `NEED:`, `REMEMBER:`, `REMEMBER PINNED:`, `FORGET`,
  `QUESTION:`) are matched at line start, case-insensitive.

---

## 7. Runtime configuration (Windows dual GPU)

### 7.1 Servers

Preferred: two `llama-server` processes (llama.cpp CUDA build), one per GPU,
because GPU affinity and context size are explicit and deterministic.

`scripts/pipeline/start-architect.ps1` (GPU 0, 16 GB):

```powershell
$env:CUDA_VISIBLE_DEVICES = "0"
llama-server.exe `
  -m "C:\models\Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf" `
  --host 127.0.0.1 --port 8081 `
  -c 32768 -ngl 99 -fa on -ctk q8_0 -ctv q8_0 `
  --cache-reuse 256 --jinja --parallel 1 `
  --alias architect
# If using a Q4 quant that does not fit: add  --n-cpu-moe 24   (tune; higher = more experts on CPU)
```

`scripts/pipeline/start-worker.ps1` (GPU 1, 8 GB):

```powershell
$env:CUDA_VISIBLE_DEVICES = "1"
llama-server.exe `
  -m "C:\models\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf" `
  --host 127.0.0.1 --port 8082 `
  -c 32768 -ngl 99 -fa on -ctk q8_0 -ctv q8_0 `
  --cache-reuse 256 --jinja --parallel 1 `
  --alias worker
```

Notes for the guide:
- CUDA device ordering can differ from Task Manager's; confirm with
  `nvidia-smi -L` and check `nvidia-smi` memory usage after launch.
- On Windows both cards are typically WDDM; the OS may reserve some VRAM.
  Leave ~1 GB headroom per card.
- Alternative: two Ollama instances (`$env:OLLAMA_HOST="127.0.0.1:11434"`
  with `CUDA_VISIBLE_DEVICES=0`, and `11435` with `=1`; set
  `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_FLASH_ATTENTION=1`,
  `OLLAMA_KV_CACHE_TYPE=q8_0`). Aider then addresses `ollama_chat/<name>`
  with per-model `api_base` in `extra_params` (verify in Phase 0; aider also
  computes `num_ctx` dynamically for Ollama, see `models.py` `send_completion`).
- LM Studio can load two models but exposes limited per-model GPU pinning;
  document as "possible, not recommended for this setup".

### 7.2 Aider model configuration

`.aider.model.settings.yml` (repo root or home):

```yaml
- name: openai/architect
  edit_format: pipeline
  use_repo_map: true
  use_temperature: 0.3          # planning benefits from lower temperature than Qwen's 0.7 default
  extra_params:
    api_base: http://127.0.0.1:8081/v1
    top_p: 0.8
    top_k: 20
    repetition_penalty: 1.05
    max_tokens: 4096

- name: openai/worker
  edit_format: editor-whole
  use_repo_map: false
  use_temperature: 0
  extra_params:
    api_base: http://127.0.0.1:8082/v1
    max_tokens: 8192
```

`.aider.model.metadata.json`:

```json
{
  "openai/architect": {"max_input_tokens": 32768, "max_output_tokens": 4096,
                       "input_cost_per_token": 0, "output_cost_per_token": 0},
  "openai/worker":    {"max_input_tokens": 32768, "max_output_tokens": 8192,
                       "input_cost_per_token": 0, "output_cost_per_token": 0}
}
```

`.aider.conf.yml`:

```yaml
pipeline: true
pipeline-architect-model: openai/architect
pipeline-worker-model: openai/worker
test-cmd: pytest -q
lint-cmd: python -m ruff check
pipeline-approve: plan
```

Environment: `setx OPENAI_API_KEY dummy` (llama-server ignores it; aider's
OpenAI provider requires a non-empty key).

For gpt-oss-20b as architect, add `reasoning_tag: think` if the server
surfaces reasoning inline, or rely on `reasoning_content` handling; set
`--reasoning-effort high` via aider or `extra_params.extra_body.reasoning_effort`.
Verify in Phase 0 that aider strips reasoning before parsing directives.

---

## 8. Prompt and format specifications

### 8.1 Architect system prompt (common core; step-specific addenda appended)

```
You are the ARCHITECT in a two-model coding pipeline. A smaller WORKER model
edits code one file at a time following your written briefs. You never edit
files yourself and you never see whole files unless you ask.

You are stateless: everything you know is in this message. Your persistent
memory is the WORKING MEMORY list below, capped at {working_memory_tokens}
tokens; the orchestrator will evict unpinned items if you exceed it.

Protocol lines you may use anywhere in a reply (one per line, at line start):
  NEED: outline <file> | refs <symbol> | grep <regex> | digest <file>[::<symbol>] | source <file>::<symbol> | source <file>:L<a>-L<b> | about <symbol>
  REMEMBER: <one line fact worth keeping for later steps>
  REMEMBER PINNED: <fact that must not be evicted>
  FORGET <memory-id>
  QUESTION: <question for the human; only if the request is genuinely ambiguous>

Ask for what you need before deciding; read it, extract what matters, and
let the rest be forgotten. Prefer outline/refs/digest over source. Keep
briefs small enough for a 7B model working on a single file.
```

Step addenda (PLAN, BRIEF, REVIEW, TEST_BRIEF, TRIAGE, COMPACT) each define
the exact output block. Full drafts belong in `pipeline_prompts.py`; the
implementer should keep them short and test them against both candidate
architect models.

### 8.2 Brief format (architect -> worker)

```markdown
# Task T2: Wire RateLimiter into ApiClient.request
File to edit: net/client.py   (this is the ONLY file you may change)

## Goal
One or two sentences.

## Changes
For each function/method, in order:
- `ApiClient.__init__(self, base_url, session=None)`: add parameter
  `rate_limiter: RateLimiter | None = None`; store as `self._rate_limiter`.
- `ApiClient.request(self, method, path, **kw)`: before sending, if
  `self._rate_limiter` is set, call `self._rate_limiter.acquire()`.
  Inputs/outputs unchanged. No other behaviour changes.

## Read-only context (do not edit; for reference)
```python
# net/ratelimit.py::RateLimiter (signature only)
class RateLimiter:
    def __init__(self, rate: float, burst: int) -> None: ...
    def acquire(self, timeout: float | None = None) -> bool: ...
```

## Constraints
- Keep existing imports; add `from net.ratelimit import RateLimiter`.
- Do not reformat unrelated code.

## Acceptance criteria
- [ ] `ApiClient.__init__` accepts `rate_limiter` keyword, default None.
- [ ] `request` calls `acquire()` exactly once when a limiter is set.
- [ ] File imports cleanly (`python -c "import net.client"`).
```

The orchestrator resolves `Read-only context` requests from the architect's
`context_snippets` list; the architect writes the list, not the code.

### 8.3 Review verdict format

```
VERDICT: ACCEPT | RETRY | REPLAN
NOTE: <one line to store on the task>
<rationale, 2-6 lines>
<for RETRY: a full revised brief in the 8.2 format>
<for REPLAN: a fenced yaml block with tasks to insert/replace>
REMEMBER: ...   (optional)
```

### 8.4 Worker digest prompt (tier 2)

```
Summarise the following code for an engineer who will not read it.
Format, max 120 tokens:
Purpose: <1 line>
Inputs/outputs: <1-2 lines>
Side effects/deps: <1 line>
Gotchas: <0-1 line>
Do not speculate about code you cannot see.
```

---

## 9. Implementation plan

Each phase is independently shippable and has acceptance criteria. Phases 1
to 4 are code; Phase 0 is configuration and measurement.

### Phase 0: dual-GPU baseline with existing aider (no code)

Deliverables: `scripts/pipeline/*.ps1|.sh`, `bench.py`, example
`.aider.model.settings.yml` / `.aider.model.metadata.json` / `.aider.conf.yml`,
draft of `aider/website/docs/llms/dual-gpu-local.md`.

Steps:
1. Launch both servers; confirm `nvidia-smi` shows each model on its own GPU;
   `curl http://127.0.0.1:808{1,2}/health`.
2. Run `aider --architect --model openai/architect --editor-model openai/worker
   --editor-edit-format editor-whole` on a toy repo; confirm per-model
   `api_base` via `extra_params` works (both `openai/` and `ollama_chat/`
   prefixes). Record any litellm quirk.
3. `bench.py`: 6 to 10 small tasks on the user's own repo (or a fixture repo),
   run with (a) Qwen3-Coder architect, (b) gpt-oss-20b architect, worker
   fixed. Record pass/fail, wall time, tokens in/out per call, prefill and
   generation tok/s. This picks the architect and sets realistic budgets.
4. Measure worker well-formedness with `editor-whole` vs `editor-diff` on
   the same tasks.

Acceptance: both models resident and addressable from one aider session;
baseline numbers recorded in the docs page.

### Phase 1: minimal pipeline loop

Scope: `PipelineCoder`, `Ledger`, `ArchitectClient` (PLAN, BRIEF, REVIEW),
`WorkerFactory` (EDIT), `verify.py` (lint + diff), COMMIT, `/pipeline
status|edit|skip|retry|abort`, `--pipeline*` flags, approval mode `plan`.
No `NEED` requests yet beyond tier 0 (repo map) and tier 1 `outline` of the
target file (needed for BRIEF).

Acceptance:
- Given a request on a fixture repo with mocked models, the coder writes a
  ledger, executes tasks in dependency order, creates one commit per accepted
  task, retries a task once when REVIEW returns RETRY, and stops with a clear
  message on the second failure.
- Architect prompt for every step is under `12.5k` tokens on a fixture with a
  1000-file repo map (assert in test via token count of the built prompt).
- Killing the process mid-run and restarting with `/pipeline resume`
  continues from the ledger.
- `pytest tests/basic/test_pipeline_*.py` green; `flake8` clean.

### Phase 2: knowledge tiers and working memory

Scope: `KnowledgeService` tiers 1 to 3, digest cache, `NEED` round-trips in
PLAN/BRIEF/REVIEW, `WorkingMemory` with REMEMBER/FORGET/PINNED, COMPACT and
eviction, `/pipeline digest`.

Acceptance:
- `NEED: refs Foo` returns grouped file:line hits from `RepoMap` tags with a
  grep fallback; `NEED: digest a.py::f` produces one worker call, a second
  identical request produces zero calls; editing `f` invalidates only its
  digest.
- Facts are absent from the next step's prompt unless a `REMEMBER:` was
  emitted (test by inspecting built prompts).
- Adding items beyond the cap triggers COMPACT then eviction; pinned items
  survive; eviction is logged.
- `/pipeline digest` reports estimated calls before running and is
  interruptible with Ctrl-C without corrupting the cache.

### Phase 3: tests and triage

Scope: `kind: test` tasks, TEST_BRIEF, WRITE_TEST worker, test run and
output trimming, TRIAGE verdicts, `--pipeline-tdd`, `max_test_rounds`.

Acceptance:
- On a fixture where the worker's first implementation is accepted and
  committed, then tests fail, TRIAGE returns FIX_CODE, a *new* task is
  inserted targeting only the code file, that task is accepted and
  committed separately, and the second test run passes. The ledger shows
  two accepted tasks and two commits; the original task's commit is
  unchanged.
- Trimmed test output never exceeds `test_output_tokens` and always contains
  the summary line and the first failure's assertion message.
- With `--pipeline-tdd`, test tasks run before their dependencies and a
  failing first run is treated as expected, not as an error.

### Phase 4: robustness and speed

Scope: overlap architect BRIEF(N+1) with worker EDIT(N) (threaded; only when
tasks are independent in the DAG); automatic `editor-whole` vs
`editor-diff` selection by file size; `max_wall_seconds` and
`max_worker_calls`; `/pipeline status` telemetry; docs polish; approval mode
`task`/`never`; reverting off-target edits.

Acceptance:
- Overlap reduces wall time on a 4-task fixture with stubbed latencies, and
  is disabled automatically when the next task depends on the current one.
- Off-target edits by the worker are reverted and reported to REVIEW.
- Documentation pages published under `aider/website/docs/`.

### Testing conventions

Follow `tests/basic/test_coder.py` patterns: `GitTemporaryDirectory`,
`InputOutput(yes=True)`, `MagicMock` for `Model.send_completion` returning
scripted architect/worker replies. Add a small fixture repo generator in
`tests/basic/pipeline_fixtures.py` producing a Python package with a few
modules and a pytest suite so `refs`/`outline`/`digest` tests are
deterministic.

---

## 10. Risks and open questions

1. **Architect quality at planning.** A 30B-A3B instruct model may produce
   under-specified briefs. Mitigations: the BRIEF prompt requires acceptance
   criteria and per-function change lines; REVIEW catches drift; the user can
   switch to gpt-oss-20b (high). If both underperform, allow a remote
   architect (`--pipeline-architect-model` accepts any aider model) while
   keeping the worker local; the design does not depend on locality.
2. **Worker edit reliability on larger files.** `editor-whole` regenerates
   the whole file; a 7B can drop or alter unrelated code. The diff review
   catches it, but retries cost time. Consider a Phase 4 "function slice"
   mode: the orchestrator extracts only the target symbols into a temporary
   file, the worker rewrites that slice, and the orchestrator splices it back.
   This bounds worker output to the function size regardless of file size.
   It is the natural extension of "one function at a time" and is called out
   here as the most valuable follow-on.
3. **Directive parsing with local models.** Expect occasional formatting
   drift. Lenient parsing plus one reflection is the plan; telemetry should
   count parse failures per model so prompts can be tuned.
4. **Token estimation drift.** Budgets are enforced with litellm's counter.
   If llama-server returns `prompt_tokens` in usage, record the ratio and
   surface a warning when the estimate is off by more than 20%.
5. **Windows process management.** The PowerShell launchers are
   convenience; aider should not manage server lifecycles. Health checks and
   a clear error ("architect endpoint not reachable at ...") are enough.
6. **Interplay with concurrent development.** All new code is in new modules
   plus small additive edits in `args.py`, `main.py`, `commands.py`,
   `coders/__init__.py`, `io.py`. Rebase risk is low. Implementation of this
   spec waits until that concurrent work is merged.
7. **Commit policy (decided).** One git commit per accepted task. No squash
   at DONE, no `/pipeline squash` command, no amending an accepted task's
   commit when later tests fail — those become a new task and a new commit.
   This keeps `/undo` granular and the ledger's `task.commit` a 1:1 map to
   git history.

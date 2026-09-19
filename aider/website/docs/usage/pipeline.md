---
parent: Usage
nav_order: 65
description: Split work between a planning model and a small editing model, each with a small context.
---

# Pipeline mode

Pipeline mode runs two models with different jobs. A stronger **architect**
plans the work, writes one brief per file and reviews every change. A smaller
**worker** does the editing: one file at a time, with an empty context for each
task.

It exists for one reason: **neither model needs a large context window.** The
architect never sees file contents, and the worker never sees more than the one
file it is changing plus a few short snippets. That makes it practical to run
both models locally on modest GPUs — see
[running two local models on two GPUs](../llms/dual-gpu-local.html).

```bash
aider --pipeline --model <architect> --editor-model <worker>
```

## How a run goes

1. **Plan.** You describe what you want. The architect breaks it into tasks,
   one file each, ordered so interfaces come before their callers. You approve
   the plan once.
2. **Brief.** For the next task, the architect writes instructions for the
   worker: what each function's inputs, outputs and behaviour should become,
   plus acceptance criteria. It works from an *outline* of the file, not its
   source.
3. **Edit.** A worker with an empty context applies that one brief to that one
   file.
4. **Review.** The architect sees the git diff and any lint output, checked
   against its own brief. It replies ACCEPT, RETRY with a corrected brief, or
   REPLAN.
5. **Commit.** Each accepted task is committed on its own.
6. **Test.** After a test task (or after every task with `--auto-test`), the
   test command runs. On failure the architect triages and adds a new task to
   fix it, which gets its own commit.

Then the worker's context is wiped and the next task starts.

## Keeping the architect's context small

The architect is **stateless**. It has no chat history. Every call is rebuilt
from:

- the repo map, ranked toward the current task
- the plan and task list from the ledger
- its working memory
- whatever it asked to look up, for this step only
- the inputs for this step (a brief, a diff, test output)

Anything it is shown is thrown away when the step ends. To carry a fact
forward it has to write a `REMEMBER:` line, which adds one short entry to its
working memory. That list has a hard token cap; when it overflows, the
architect is asked to compact it, and anything unpinned is evicted oldest
first.

This is deliberate. A human reading unfamiliar code keeps a handful of facts
and forgets the rest, and that is the behaviour that keeps a long run inside a
small context window.

## How the architect learns about the repo

It pulls what it needs with `NEED:` lines, cheapest source first:

| Request | What it gets | Cost |
|---|---|---|
| `NEED: outline <file>` | every definition in the file with line numbers | free |
| `NEED: refs <symbol>` | where the symbol is used | free |
| `NEED: trace <symbol>` | callers and callees as snippets | free |
| `NEED: grep <regex>` | text search across the repo | free |
| `NEED: digest <file>::<symbol>` | a short summary written by the worker, cached on disk | one worker call, once |
| `NEED: source <file>::<symbol>` | the actual code for one symbol | costs architect context |
| `NEED: about <symbol>` | references plus a digest, when it doesn't know where to start | maybe one worker call |

Digests are cached by content hash, so editing one function only invalidates
that function's digest. If you want to prime the cache up front:

```
/pipeline digest src/parser.py src/lexer.py
```

That is optional. It is usually not worth summarising a whole repo, because
most of it is irrelevant to any one request and the repo map already gives the
structure for free.

## Commands

| Command | What it does |
|---|---|
| `/pipeline <request>` | plan and work through a request |
| `/pipeline` | switch the chat into pipeline mode |
| `/pipeline status` | the task table, token use and memory use |
| `/pipeline resume` | continue from the ledger, after a stop or a crash |
| `/pipeline skip <id>` | mark a task as skipped |
| `/pipeline retry <id>` | put a failed task back to pending |
| `/pipeline abort` | stop after the current step |
| `/pipeline edit` | open the ledger in your editor |
| `/pipeline digest <paths>` | pre-compute digests for some files |

## State on disk

Everything lives in `.aider.pipeline/` in your repo (already covered by
aider's `.aider*` gitignore rule):

```
.aider.pipeline/
  ledger.yml        the request, plan, task statuses, working memory, history
  briefs/T1.md      the brief each task was given
  digests/<hash>.md cached worker summaries
```

The ledger is plain YAML, so you can read it, edit it with `/pipeline edit`,
and resume. Because every accepted task is its own commit, `/undo` and
`git revert` work per task.

## Approvals

- `--pipeline-approve plan` (default) asks you once, after the plan.
- `--pipeline-approve task` also shows each brief before the worker runs and
  each verdict after review.
- `--pipeline-approve never` runs start to finish without asking. The
  architect's questions get answered with "use your best judgement".

The architect can ask you a question with a `QUESTION:` line at any point, and
your answer is recorded in the ledger.

## Tuning

Every prompt section has its own cap, so you can trade context for detail. The
defaults suit a 32k window:

```bash
aider --pipeline \
  --pipeline-architect-map-tokens 2000 \
  --pipeline-working-memory-tokens 2000 \
  --pipeline-facts-tokens 3000 \
  --pipeline-review-diff-tokens 2500 \
  --pipeline-worker-snippet-tokens 2000
```

Run `aider --help` for the full list, including `--pipeline-max-tasks`,
`--pipeline-max-attempts`, `--pipeline-max-test-rounds` and
`--pipeline-max-worker-calls`.

Other options worth knowing:

- `--pipeline-tdd` writes test tasks before the code they cover, and treats the
  first failing run as expected.
- `--pipeline-whole-file-max-tokens` sets the size above which the worker
  switches from rewriting whole files to search/replace edits. Whole-file
  rewrites are much more reliable for small models, but slow on large files.
- `--no-pipeline-prewarm` skips loading both models at startup.

## Tests and linting

Pipeline mode uses your existing `--test-cmd` and `--lint-cmd`. Lint output
goes to the architect as part of every review. Test output is trimmed to the
summary plus the first failure before the architect sees it, so a noisy test
run cannot flood its context.

## When not to use it

For a one-line change, plain `--architect` or code mode is faster: pipeline
mode spends calls on planning, briefing and reviewing. It pays off on changes
that touch several files, and whenever your models have small context windows.

# How to use pipeline mode on two GPUs

Pipeline mode splits aider across **two models**:

- An **architect** plans the work, writes one brief per file, and reviews
  each change.
- A **worker** edits one file at a time, with an empty context for every
  task.

That split is aimed at a local dual-GPU box: a 16 GB card running a stronger
model, and an 8 GB card running a smaller coder. Each model keeps a small
context window. Normal aider is unchanged until you pass `--pipeline`.

Website copies of this guide:

- [Pipeline mode](../aider/website/docs/usage/pipeline.md) — what a run does
- [Two local models on two GPUs](../aider/website/docs/llms/dual-gpu-local.md)
  — servers, VRAM, Ollama

## When to use it

Use pipeline mode when:

- you have two GPUs (or two local servers) and small context windows
- the change touches several files and you want one git commit per file

Skip it for a one-line fix. Plain code mode or `--architect` is faster: a
pipeline run spends extra calls on planning, briefing and reviewing.

## Quick start

1. Confirm which GPU is which:

   ```
   nvidia-smi -L
   ```

2. Start one `llama-server` per GPU. From this checkout:

   Windows:

   ```powershell
   .\scripts\pipeline\start-both.ps1 `
     -ArchitectModel C:\models\Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf `
     -WorkerModel C:\models\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf
   ```

   Linux / macOS:

   ```bash
   ARCHITECT_MODEL=~/models/Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf \
   WORKER_MODEL=~/models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf \
   ./scripts/pipeline/start-both.sh
   ```

   The scripts wait until each server answers `/health`, then print the
   aider command. Check `nvidia-smi` that each model landed on its own card.

3. Point aider at both. llama-server ignores the API key, but a value is
   still required:

   ```bash
   export OPENAI_API_KEY=dummy

   aider --pipeline \
     --pipeline-architect-model openai/architect \
     --pipeline-architect-api-base http://127.0.0.1:8081/v1 \
     --pipeline-worker-model openai/worker \
     --pipeline-worker-api-base http://127.0.0.1:8082/v1
   ```

Suggested models and quants are in
[two local models on two GPUs](../aider/website/docs/llms/dual-gpu-local.md).
In short: Qwen3-Coder-30B-A3B (Q3) on the 16 GB card, Qwen2.5-Coder-7B (Q4)
on the 8 GB card. gpt-oss-20b is a slower, more deliberate architect if you
prefer that.

## A session

At the `pipeline>` prompt, describe the change the way you would in code
mode:

```
pipeline> Clamp negative factors to zero in scale(), and cover it with a test.
```

What happens:

1. **Plan.** The architect breaks the request into tasks, one file each.
   You approve that plan once (`--pipeline-approve plan`, the default).
2. **Brief.** For the next task it writes instructions for the worker from
   an *outline* of the file, not the source.
3. **Edit.** The worker applies that brief to that one file, with no chat
   history from earlier tasks.
4. **Review.** The architect sees a token-capped git diff of that file and
   replies ACCEPT, RETRY (with a corrected brief), or REPLAN.
5. **Commit.** Each accepted task is committed on its own. Nothing is
   squashed, and an accepted commit is never amended.
6. **Test.** After a test task (or after every task with `--auto-test`),
   your `--test-cmd` runs. A failure becomes a **new** task with its own
   commit.

Then the worker's context is wiped and the next task starts.

You can also start a run from any chat mode:

```
/pipeline Clamp negative factors to zero in scale()
```

`/pipeline` with no arguments just switches into pipeline mode.

## Commands

| Command | What it does |
|---|---|
| `/pipeline <request>` | plan and work through a request |
| `/pipeline` | switch into pipeline mode |
| `/pipeline status` | task table, token use, working memory |
| `/pipeline resume` | continue from the ledger after a stop or crash |
| `/pipeline skip <id>` | mark a task skipped |
| `/pipeline retry <id>` | put a failed task back to pending |
| `/pipeline abort` | stop after the current step |
| `/pipeline edit` | open the ledger in your editor |
| `/pipeline digest <paths>` | pre-compute short summaries of some files |

## Approvals

- `--pipeline-approve plan` (default) — ask once, after the plan.
- `--pipeline-approve task` — also show each brief and each review verdict.
- `--pipeline-approve never` — run unattended. Architect questions are
  answered with "use your best judgement". If it tries to accept a known
  test failure with nobody there to confirm, the run stops.

## Config you can keep

`.aider.conf.yml` in the repo:

```yaml
pipeline: true
pipeline-architect-model: openai/architect
pipeline-architect-api-base: http://127.0.0.1:8081/v1
pipeline-worker-model: openai/worker
pipeline-worker-api-base: http://127.0.0.1:8082/v1
test-cmd: pytest -q
lint-cmd: python -m ruff check
```

Or pass `--pipeline` only when you want it, and leave the api_base values
in the config. `--model` / `--editor-model` can stand in for the architect
and worker if you do not set `--pipeline-*-model`.

Tell aider each model's window in `.aider.model.metadata.json` so it can
warn before a prompt is too large — examples are on the
[dual-GPU page](../aider/website/docs/llms/dual-gpu-local.md).

## What appears on disk

```
.aider.pipeline/
  ledger.yml        request, plan, task statuses, working memory, history
  briefs/T1.md      the brief each task was given
  digests/<hash>.md cached worker summaries
```

`.aider*` is already offered in `.gitignore`. The ledger is YAML: you can
read it, edit it with `/pipeline edit`, and `/pipeline resume`. Because
every accepted task is its own commit, `/undo` and `git revert` work per
task.

## Flags worth knowing

```bash
aider --pipeline \
  --pipeline-tdd \
  --pipeline-whole-file-max-tokens 3000 \
  --pipeline-architect-map-tokens 2000 \
  --pipeline-working-memory-tokens 2000
```

- `--pipeline-tdd` writes test tasks before the code they cover, and treats
  the first failing run as expected.
- `--pipeline-whole-file-max-tokens` is the size above which the worker
  switches from rewriting the whole file to search/replace. Whole-file
  rewrites are more reliable for small models.
- `--pipeline-prewarm` sends a real request to both models at startup so
  they load into VRAM. It is **off** unless both api_base values look local
  (`127.0.0.1` / `localhost`), so a hosted API is not billed by surprise.
- Token caps (`--pipeline-*-tokens`) bound every section of the architect
  prompt. Defaults suit a 32k window. `aider --help` lists them all.

## Smoke test (no GPU)

```bash
python scripts/pipeline/smoke-two-endpoints.py
```

That stands up two fake OpenAI endpoints and checks that aider talks to
both, edits a file, and makes one commit per accepted task.

## Troubleshooting

**"architect endpoint not reachable"** — the server is not up, or the port
is wrong. On Linux the launcher logs to `/tmp/aider-pipeline-architect.log`.

**Both models on one card** — set `CUDA_VISIBLE_DEVICES` per process (the
launcher scripts do this). Confirm with `nvidia-smi`.

**Malformed worker edits** — raise `--pipeline-whole-file-max-tokens` so
more files are rewritten whole, or split the file.

**Every task feels like a cold start** — keep `--cache-reuse` on both
servers (the launchers set 256) and check nothing else is using the VRAM.
`/pipeline status` shows where time and tokens went.

**Unattended run went green with failing tests** — that was a bug; with
`--pipeline-approve never`, a leftover failure now stops the run instead of
being silently accepted.

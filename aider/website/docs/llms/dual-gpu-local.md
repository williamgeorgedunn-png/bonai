---
parent: Connecting to LLMs
nav_order: 450
---

# Two local models on two GPUs

[Pipeline mode](../usage/pipeline.html) runs a planning **architect** and a
small editing **worker**. This page sets both up as local servers, one per GPU,
on Windows or Linux.

The point of the split is that each model gets a small context window and a
narrow job, so a 16 GB card and an 8 GB card together can do work that would
otherwise need a much larger machine.

## Picking models

| Role | Model | Quant | VRAM | Notes |
|---|---|---|---|---|
| Architect | Qwen3-Coder-30B-A3B-Instruct | Q3_K_M / IQ3 | ~13-15 GB | Fits a 16 GB card with a 32k context and `q8_0` KV cache. Code-trained and fast: only 3.3B parameters are active per token. |
| Architect | Qwen3-Coder-30B-A3B-Instruct | Q4_K_M | ~18.6 GB | Does **not** fit 16 GB on its own. Use `--n-cpu-moe` to push expert layers to the CPU. |
| Architect | gpt-oss-20b | MXFP4 | ~12 GB | Fits comfortably with room for a large context, and reasons more deliberately at high reasoning effort. Slower per step. Needs `--jinja` for its chat template. |
| Worker | Qwen2.5-Coder-7B-Instruct | Q4_K_M | ~4.7 GB | Fits 8 GB with a 32k context. Good at following a precise brief for one file. |
| Worker | Qwen2.5-Coder-14B-Instruct | Q4_K_M | ~9 GB | Too big for 8 GB. |

Check the numbers against the actual GGUF files you download; quant sizes move
around between releases. Leave about 1 GB of headroom per card, more on
Windows, where the OS reserves some VRAM.

Worth benchmarking both architect candidates on your own repo before settling.
The worker is the quality bottleneck in practice, so a stronger worker often
helps more than a stronger architect.

## Start the servers

Two `llama-server` processes, each pinned to one GPU. Confirm which card is
which first, because CUDA's ordering does not always match what Task Manager
shows:

```
nvidia-smi -L
```

On Windows:

```powershell
# From the aider checkout
.\scripts\pipeline\start-both.ps1 `
  -ArchitectModel C:\models\Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf `
  -WorkerModel C:\models\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf
```

On Linux or macOS:

```bash
ARCHITECT_MODEL=~/models/Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf \
WORKER_MODEL=~/models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf \
./scripts/pipeline/start-both.sh
```

Both scripts wait for each server's `/health` endpoint and then print the aider
command to run. If a Q4 MoE architect quant will not fit, pass
`-CpuMoeLayers 24` (or `CPU_MOE_LAYERS=24`) and tune the number down until it
stops fitting.

Then check each model landed on its own card:

```
nvidia-smi
```

## Point aider at both

The two endpoints can go straight on the command line. No config files needed:

```bash
setx OPENAI_API_KEY dummy      # llama-server ignores it, but a value is required

aider --pipeline \
  --pipeline-architect-model openai/architect \
  --pipeline-architect-api-base http://127.0.0.1:8081/v1 \
  --pipeline-worker-model openai/worker \
  --pipeline-worker-api-base http://127.0.0.1:8082/v1
```

Put it in `.aider.conf.yml` so you don't retype it:

```yaml
pipeline: true
pipeline-architect-model: openai/architect
pipeline-architect-api-base: http://127.0.0.1:8081/v1
pipeline-worker-model: openai/worker
pipeline-worker-api-base: http://127.0.0.1:8082/v1
test-cmd: pytest -q
lint-cmd: python -m ruff check
```

Aider does not know these model names, so tell it their limits in
`.aider.model.metadata.json`, which stops it guessing and lets it warn you
before a prompt is too large:

```json
{
  "openai/architect": {
    "max_input_tokens": 32768,
    "max_output_tokens": 4096,
    "input_cost_per_token": 0,
    "output_cost_per_token": 0
  },
  "openai/worker": {
    "max_input_tokens": 32768,
    "max_output_tokens": 8192,
    "input_cost_per_token": 0,
    "output_cost_per_token": 0
  }
}
```

Sampling settings go in `.aider.model.settings.yml`. Planning benefits from a
lower temperature than Qwen's default, and the worker should be close to
deterministic:

```yaml
- name: openai/architect
  edit_format: pipeline
  use_repo_map: true
  use_temperature: 0.3
  extra_params:
    top_p: 0.8
    top_k: 20
    max_tokens: 4096

- name: openai/worker
  edit_format: pipeline-worker-whole
  use_repo_map: false
  use_temperature: 0
  extra_params:
    max_tokens: 8192
```

You can also set `api_base` per model in this file instead of using the command
line flags. The flags exist so a two-GPU setup needs no config file at all.

## Keeping hand-offs fast

The worker's context is wiped between tasks, but that should not feel like a
cold start. Three things matter:

1. **Aider reuses the worker.** It resets the message history and the file set
   on one worker instead of building a new one, so no hand-off re-scans the
   repo or reloads model metadata.
2. **The worker's system prompt never changes between tasks.** With
   `--cache-reuse 256` (set by the launcher scripts) llama.cpp keeps the KV
   cache for that unchanged prefix instead of re-reading it every time. The
   architect's prompt is built stable-prefix-first for the same reason.
3. **Both models stay loaded.** Aider pings each one at startup so the first
   task doesn't pay to load weights. Keep `--parallel 1` on both servers: two
   concurrent slots halve the context each request can use.

If you use Ollama instead of llama.cpp, set `OLLAMA_KEEP_ALIVE=-1` so it does
not unload a model after five idle minutes, which would otherwise make the
first task after a long review very slow.

## Using Ollama instead

Two instances, one per GPU:

```powershell
# First window: architect on GPU 0
$env:CUDA_VISIBLE_DEVICES = "0"
$env:OLLAMA_HOST = "127.0.0.1:11434"
$env:OLLAMA_KEEP_ALIVE = "-1"
$env:OLLAMA_NUM_PARALLEL = "1"
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KV_CACHE_TYPE = "q8_0"
$env:OLLAMA_CONTEXT_LENGTH = "32768"
ollama serve

# Second window: worker on GPU 1
$env:CUDA_VISIBLE_DEVICES = "1"
$env:OLLAMA_HOST = "127.0.0.1:11435"
# ... same settings as above
ollama serve
```

Then:

```bash
aider --pipeline \
  --pipeline-architect-model ollama_chat/qwen3-coder:30b \
  --pipeline-architect-api-base http://127.0.0.1:11434 \
  --pipeline-worker-model ollama_chat/qwen2.5-coder:7b \
  --pipeline-worker-api-base http://127.0.0.1:11435
```

Note that Ollama silently drops context beyond its window, so set
`OLLAMA_CONTEXT_LENGTH` explicitly rather than relying on the default 2k. See
[the Ollama page](ollama.html) for more.

LM Studio can load two models at once but gives you little control over which
GPU each one uses, so it is a poor fit for this setup.

## Troubleshooting

**"architect endpoint not reachable"** — the server isn't up, or the port is
wrong. Check its window, or `/tmp/aider-pipeline-architect.log` on Linux.

**Both models ended up on one card** — `CUDA_VISIBLE_DEVICES` is set per
process, so each server must be launched from its own shell. Verify with
`nvidia-smi` rather than trusting the launcher output.

**The worker produces malformed edits** — small models are much more reliable
rewriting a whole file than producing search/replace blocks. Raise
`--pipeline-whole-file-max-tokens` so more files use whole-file rewrites, or
split the file.

**Every task is slow to start** — check `--cache-reuse` is set on both
servers and that nothing else is competing for VRAM. Run
`/pipeline status` to see where the time and tokens are going.

**Prompts are larger than you expected** — aider estimates local token counts
rather than using the model's own tokenizer, so leave margin. Lower the
`--pipeline-*-tokens` budgets if you are hitting the context limit.

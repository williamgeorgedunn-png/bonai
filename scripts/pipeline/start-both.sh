#!/usr/bin/env bash
# Start both pipeline model servers, one per GPU, and wait until they answer.
# Linux/macOS equivalent of start-both.ps1.
set -euo pipefail

ARCHITECT_MODEL="${ARCHITECT_MODEL:-$HOME/models/Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf}"
WORKER_MODEL="${WORKER_MODEL:-$HOME/models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf}"
ARCHITECT_PORT="${ARCHITECT_PORT:-8081}"
WORKER_PORT="${WORKER_PORT:-8082}"
ARCHITECT_GPU="${ARCHITECT_GPU:-0}"
WORKER_GPU="${WORKER_GPU:-1}"
CONTEXT="${CONTEXT:-32768}"
# Set CPU_MOE_LAYERS if a Q4 MoE quant does not fit on the architect card.
CPU_MOE_LAYERS="${CPU_MOE_LAYERS:-0}"
TIMEOUT="${TIMEOUT:-300}"

common_args=(
  --host 127.0.0.1
  -c "$CONTEXT"
  -ngl 99
  -fa on
  -ctk q8_0
  -ctv q8_0
  --cache-reuse 256
  --jinja
  --parallel 1
)

architect_args=("${common_args[@]}" --alias architect)
if [ "$CPU_MOE_LAYERS" -gt 0 ]; then
  architect_args+=(--n-cpu-moe "$CPU_MOE_LAYERS")
fi

CUDA_VISIBLE_DEVICES="$ARCHITECT_GPU" llama-server \
  -m "$ARCHITECT_MODEL" --port "$ARCHITECT_PORT" "${architect_args[@]}" \
  >/tmp/aider-pipeline-architect.log 2>&1 &
architect_pid=$!

CUDA_VISIBLE_DEVICES="$WORKER_GPU" llama-server \
  -m "$WORKER_MODEL" --port "$WORKER_PORT" "${common_args[@]}" --alias worker \
  >/tmp/aider-pipeline-worker.log 2>&1 &
worker_pid=$!

wait_for() {
  local name=$1 port=$2 waited=0
  printf 'Waiting for %s on port %s ' "$name" "$port"
  while [ "$waited" -lt "$TIMEOUT" ]; do
    if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      echo "ready"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
    printf '.'
  done
  echo "timed out"
  return 1
}

if ! wait_for architect "$ARCHITECT_PORT"; then
  echo "See /tmp/aider-pipeline-architect.log" >&2
  exit 1
fi
if ! wait_for worker "$WORKER_PORT"; then
  echo "See /tmp/aider-pipeline-worker.log" >&2
  exit 1
fi

cat <<EOF

Both models are loaded (architect pid $architect_pid, worker pid $worker_pid).
Check VRAM use per card with: nvidia-smi

Now run aider:

  aider --pipeline \\
    --pipeline-architect-model openai/architect \\
    --pipeline-architect-api-base http://127.0.0.1:$ARCHITECT_PORT/v1 \\
    --pipeline-worker-model openai/worker \\
    --pipeline-worker-api-base http://127.0.0.1:$WORKER_PORT/v1
EOF

# Start the worker model on GPU 1 (the smaller card).
#
# Check which card is which first:  nvidia-smi -L

param(
    [string]$Model = "$env:USERPROFILE\models\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
    [int]$Port = 8082,
    [int]$Context = 32768,
    [int]$Gpu = 1
)

$env:CUDA_VISIBLE_DEVICES = "$Gpu"

$arguments = @(
    "-m", $Model,
    "--host", "127.0.0.1",
    "--port", "$Port",
    "-c", "$Context",
    "-ngl", "99",
    "-fa", "on",
    "-ctk", "q8_0",
    "-ctv", "q8_0",
    # The worker's context is wiped between tasks but its system prompt never
    # changes, so the prefix cache saves the prefill on every hand-off.
    "--cache-reuse", "256",
    "--jinja",
    "--parallel", "1",
    "--alias", "worker"
)

Write-Host "Worker on GPU $Gpu, port $Port, $Context token context"
& llama-server.exe @arguments

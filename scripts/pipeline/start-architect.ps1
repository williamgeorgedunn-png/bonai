# Start the architect model on GPU 0 (the larger card).
#
# Check which card is which first:  nvidia-smi -L
# CUDA's device order does not always match Task Manager's.

param(
    [string]$Model = "$env:USERPROFILE\models\Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf",
    [int]$Port = 8081,
    [int]$Context = 32768,
    [int]$Gpu = 0,
    # Set this if a Q4 quant does not fit: it moves that many MoE expert layers
    # to the CPU. Higher means less VRAM and slower generation.
    [int]$CpuMoeLayers = 0
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
    # Reuse the prompt prefix across calls. The pipeline keeps the architect's
    # system prompt identical between steps so this actually pays off.
    "--cache-reuse", "256",
    "--jinja",
    "--parallel", "1",
    "--alias", "architect"
)

if ($CpuMoeLayers -gt 0) {
    $arguments += @("--n-cpu-moe", "$CpuMoeLayers")
}

Write-Host "Architect on GPU $Gpu, port $Port, $Context token context"
& llama-server.exe @arguments

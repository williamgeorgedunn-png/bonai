# Start both pipeline model servers in their own windows and wait until they
# answer. Run this once, then run aider in a third window.

param(
    [string]$ArchitectModel,
    [string]$WorkerModel,
    [int]$ArchitectPort = 8081,
    [int]$WorkerPort = 8082,
    [int]$ArchitectGpu = 0,
    [int]$WorkerGpu = 1,
    [int]$CpuMoeLayers = 0,
    [int]$TimeoutSeconds = 300
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Start-Server($script, $arguments) {
    $command = "& '$here\$script' $arguments"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $command | Out-Null
}

$architectArgs = "-Port $ArchitectPort -Gpu $ArchitectGpu -CpuMoeLayers $CpuMoeLayers"
if ($ArchitectModel) { $architectArgs += " -Model '$ArchitectModel'" }
Start-Server "start-architect.ps1" $architectArgs

$workerArgs = "-Port $WorkerPort -Gpu $WorkerGpu"
if ($WorkerModel) { $workerArgs += " -Model '$WorkerModel'" }
Start-Server "start-worker.ps1" $workerArgs

function Wait-Server($name, $port) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    Write-Host "Waiting for $name on port $port ..." -NoNewline
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing `
                -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                Write-Host " ready"
                return $true
            }
        } catch {
            Start-Sleep -Seconds 3
            Write-Host "." -NoNewline
        }
    }
    Write-Host " timed out"
    return $false
}

$architectUp = Wait-Server "architect" $ArchitectPort
$workerUp = Wait-Server "worker" $WorkerPort

if (-not ($architectUp -and $workerUp)) {
    Write-Warning "A server did not come up. Check its window for the error."
    exit 1
}

Write-Host ""
Write-Host "Both models are loaded. Check VRAM use per card with: nvidia-smi"
Write-Host ""
Write-Host "Now run aider, for example:"
Write-Host ""
Write-Host "  aider --pipeline ``"
Write-Host "    --pipeline-architect-model openai/architect ``"
Write-Host "    --pipeline-architect-api-base http://127.0.0.1:$ArchitectPort/v1 ``"
Write-Host "    --pipeline-worker-model openai/worker ``"
Write-Host "    --pipeline-worker-api-base http://127.0.0.1:$WorkerPort/v1"

# Start both services locally (Windows PowerShell): no Docker, no MySQL.
#   .\scripts\run_dev.ps1
#   $env:BACKEND_DRIVER="http"; .\scripts\run_dev.ps1
# Stop with Ctrl+C; both processes are cleaned up in the finally block.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$AgentPort = if ($env:AGENT_PORT) { $env:AGENT_PORT } else { "8000" }
$BusinessPort = if ($env:BUSINESS_PORT) { $env:BUSINESS_PORT } else { "8001" }
if (-not $env:BACKEND_DRIVER) { $env:BACKEND_DRIVER = "inprocess" }

$business = Start-Process -PassThru -NoNewWindow uv -ArgumentList `
    "run", "python", "-m", "uvicorn", "shopping_backend.business_api.main:app", "--host", "127.0.0.1", "--port", $BusinessPort
$agent = Start-Process -PassThru -NoNewWindow uv -ArgumentList `
    "run", "python", "-m", "uvicorn", "shopping_backend.agent_api.main:app", "--host", "127.0.0.1", "--port", $AgentPort

Write-Host ""
Write-Host "  agent     http://127.0.0.1:$AgentPort   (BACKEND_DRIVER=$env:BACKEND_DRIVER)"
Write-Host "  business  http://127.0.0.1:$BusinessPort"
Write-Host "  docs      http://127.0.0.1:$AgentPort/docs"
Write-Host ""
Write-Host "  verify:  uv run python scripts\run_demo.py"
Write-Host ""

try {
    while ($true) {
        if ($agent.HasExited -or $business.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
} finally {
    foreach ($process in @($agent, $business)) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
}

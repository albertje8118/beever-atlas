# dev.ps1 — Start backend + frontend together
# Usage: from the project root:  .\plugins\dev.ps1
# Press Ctrl+C to stop both servers.

$root = Split-Path $PSScriptRoot -Parent

Write-Host ""
Write-Host "  beever-atlas dev servers" -ForegroundColor Cyan
Write-Host "  Backend  → http://localhost:8000" -ForegroundColor Green
Write-Host "  Frontend → http://localhost:5173" -ForegroundColor Green
Write-Host ""
Write-Host "  Press Ctrl+C to stop both." -ForegroundColor Yellow
Write-Host ""

try {
    $copilotToken = gh auth token
    if ($LASTEXITCODE -eq 0 -and $copilotToken) {
        $tokenValue = $copilotToken.Trim()
        $env:COPILOT_GITHUB_TOKEN = $tokenValue
        $env:GITHUB_TOKEN = $tokenValue
        Write-Host "  Copilot token  → loaded from gh auth" -ForegroundColor Green
        Write-Host ""
    }
} catch {
    Write-Host "  Copilot token  → not available from gh auth; backend will rely on existing env vars" -ForegroundColor Yellow
    Write-Host ""
}

# Start backend
$backend = Start-Process -FilePath "uv" `
    -ArgumentList "run", "uvicorn", "start_with_plugins:app", "--reload", "--host", "0.0.0.0", "--port", "8000" `
    -WorkingDirectory $root `
    -PassThru -NoNewWindow

# Start frontend through the plugin wrapper so the ChatGPT overlay loads
$frontend = Start-Process -FilePath "node" `
    -ArgumentList "plugins/web/run-vite.mjs", "dev" `
    -WorkingDirectory $root `
    -PassThru -NoNewWindow

# Wait and clean up on Ctrl+C
try {
    while ($true) { Start-Sleep -Seconds 1 }
} finally {
    function Stop-ProcessTree {
        param(
            [System.Diagnostics.Process]$Process
        )

        if ($null -eq $Process) {
            return
        }

        try {
            if ($Process.HasExited) {
                return
            }
        } catch {
            return
        }

        $pid = $Process.Id
        $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
        if ($taskkill) {
            & $taskkill.Source /PID $pid /T /F | Out-Null
            return
        }

        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }

    Write-Host "`nStopping servers..." -ForegroundColor Yellow
    Stop-ProcessTree -Process $backend
    Stop-ProcessTree -Process $frontend
    Write-Host "Done." -ForegroundColor Green
}

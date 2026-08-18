$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$frontendDir = Join-Path $repoRoot "frontend"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

Write-Host "Starting ErrAgent backend on http://127.0.0.1:8006"
Start-Process powershell `
  -ArgumentList "-NoExit", "-Command", "cd '$repoRoot'; & '$venvPython' -m uvicorn backend.app.app:app --host 127.0.0.1 --port 8006" `
  -WorkingDirectory $repoRoot

Write-Host "Starting ErrAgent frontend on http://127.0.0.1:8086"
Start-Process powershell `
  -ArgumentList "-NoExit", "-Command", "cd '$frontendDir'; npm run dev -- --host 127.0.0.1 --port 8086" `
  -WorkingDirectory $repoRoot

Write-Host "Both apps are starting."
Write-Host "Backend: http://127.0.0.1:8006"
Write-Host "Frontend: http://127.0.0.1:8086"

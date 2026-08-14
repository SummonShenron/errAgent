$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $repoRoot "backend"
$frontendDir = Join-Path $repoRoot "frontend"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

Write-Host "Starting ErrAgent backend on http://127.0.0.1:8000"
Start-Process powershell `
  -ArgumentList "-NoExit", "-Command", "cd '$backendDir'; & '$venvPython' -m uvicorn app.app:app --host 127.0.0.1 --port 8000" `
  -WorkingDirectory $repoRoot

Write-Host "Starting ErrAgent frontend on http://127.0.0.1:8080"
Start-Process powershell `
  -ArgumentList "-NoExit", "-Command", "cd '$frontendDir'; npm run dev -- --host 127.0.0.1 --port 8080" `
  -WorkingDirectory $repoRoot

Write-Host "Both apps are starting."
Write-Host "Backend: http://127.0.0.1:8000"
Write-Host "Frontend: http://127.0.0.1:8080"

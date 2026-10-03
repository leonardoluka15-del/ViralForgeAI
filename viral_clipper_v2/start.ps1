$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "Viral Clipper - local launcher" -ForegroundColor Cyan
try {
  docker version | Out-Null
} catch {
  Write-Host "Docker Desktop is not running. Start Docker Desktop, wait for Engine Running, then run this file again." -ForegroundColor Red
  exit 1
}
try {
  $tags = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 2
  Write-Host "Ollama detected." -ForegroundColor Green
} catch {
  Write-Host "Ollama is not reachable. The app will still work with heuristic viral scoring." -ForegroundColor Yellow
}
docker compose up --build

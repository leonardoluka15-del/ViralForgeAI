@echo off
cd /d %~dp0
echo Updating Viral Clipper from GitHub...
git pull origin viral-clipper-v2
if errorlevel 1 (
  echo Git pull failed. Fix the Git error shown above, then retry.
  pause
  exit /b 1
)
echo Starting Docker...
docker compose down
docker compose up --build
pause

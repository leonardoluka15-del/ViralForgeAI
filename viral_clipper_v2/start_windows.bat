@echo off
cd /d %~dp0
echo Starting Viral Clipper...
docker compose up --build
pause

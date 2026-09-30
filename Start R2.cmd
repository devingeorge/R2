@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup first: python -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  pause
  exit /b 1
)
echo Open R2 at http://127.0.0.1:8765. Press Ctrl+C to stop.
".venv\Scripts\python.exe" -m uvicorn r2.server:app --host 127.0.0.1 --port 8765 --no-access-log

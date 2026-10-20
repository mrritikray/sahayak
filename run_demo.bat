@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please complete the README setup first: create .venv and install requirements.
  pause
  exit /b 1
)
echo Starting SAHAYAK in TWO windows: the scheduler and the web app.
echo Close both windows to stop SAHAYAK completely.
start "SAHAYAK Scheduler" cmd /k ".venv\Scripts\python.exe -m flask --app app worker"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:5000
.venv\Scripts\python.exe app.py
pause

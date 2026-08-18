@echo off
cd /d "%~dp0"

REM Read ports from config.json (defaults: 8080 / 8081).
set "HTTP_PORT=8080"
set "STREAM_PORT=8081"
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "(Get-Content -Raw config.json | ConvertFrom-Json).http_port"`) do set "HTTP_PORT=%%p"
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "(Get-Content -Raw config.json | ConvertFrom-Json).stream_port"`) do set "STREAM_PORT=%%p"

REM Kill any process listening on the configured ports.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%HTTP_PORT%" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%STREAM_PORT%" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)

start "TTS Engine" cmd /k "python app.py"
timeout /t 3 /nobreak > NUL
start "" "http://127.0.0.1:%HTTP_PORT%/index.html?v=%RANDOM%"


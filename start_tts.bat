@echo off
cd /d "%~dp0"

REM Close stale server on this port so we don't serve an old instance.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8081" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)

start "TTS Engine" cmd /k "python app.py"
timeout /t 3 /nobreak > NUL
start "" "http://127.0.0.1:8080/index.html?v=%RANDOM%"

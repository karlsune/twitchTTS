@echo off
cd /d "%~dp0"
start "TTS Engine" cmd /k "python app.py"
timeout /t 2 /nobreak > NUL
start http://localhost:8080/index.html

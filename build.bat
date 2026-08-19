@echo off
REM Build a standalone twitchTTS.exe (run from the repo root).
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name twitchTTS ^
  --add-data "index.html;." ^
  --add-data "styles.css;." ^
  --add-data "config.example.json;." ^
  --hidden-import pystray._win32 ^
  twitchtts_app.py
echo.
echo Built: dist\twitchTTS.exe

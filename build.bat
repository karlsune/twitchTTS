@echo off
REM Build a standalone twitchTTS.exe (run from the repo root).
REM Prefer the project virtual environment: the system Python may lack
REM pystray/PIL, and PyInstaller silently skips missing imports.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)
%PY% -m pip install --upgrade pyinstaller
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name twitchTTS ^
  --add-data "index.html;." ^
  --add-data "styles.css;." ^
  --add-data "config.example.json;." ^
  --add-data "LICENSE.md;." ^
  --add-data "THIRD-PARTY-SOFTWARE.md;." ^
  --add-data "NOTICE;." ^
  --hidden-import cffi ^
  --hidden-import _cffi_backend ^
  --hidden-import pystray._win32 ^
  twitchtts_app.py
echo.
echo Built: dist\twitchTTS.exe

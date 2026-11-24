@echo off
cd /d "%~dp0"
title Clipdesk install
echo === Clipdesk install ===
where python >nul 2>&1 || (echo Python 3.10+ not found. Install it from python.org, tick "Add to PATH", then run this again. & pause & exit /b 1)
where ffmpeg >nul 2>&1 || (echo ffmpeg not found. Run: winget install ffmpeg   then open a new window and run this again. & pause & exit /b 1)
where git >nul 2>&1 || echo (git not found, only needed if you want to update with git pull)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
if not exist creators.json copy creators.example.json creators.json >nul
echo.
echo Next steps:
echo  1. Install the Claude CLI and sign in:  npm install -g @anthropic-ai/claude-code   then run  claude  once and log in
echo  2. Read docs\youtube.md and docs\instagram.md
echo  3. Double-click run_clipdesk.bat, the app opens at http://127.0.0.1:8765
pause

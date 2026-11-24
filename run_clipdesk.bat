@echo off
cd /d "%~dp0"
title Clipdesk (keep this window open)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1
timeout /t 2 >nul
powershell -NoProfile -Command "python -u server.py 2>&1 | Tee-Object -FilePath clipdesk_log.txt"
echo.
echo Clipdesk stopped.
timeout /t 30 >nul

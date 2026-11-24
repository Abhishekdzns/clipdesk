@echo off
cd /d "%~dp0"
title Connect YouTube (keep open)
if not exist client_secret.json (
  for /f "delims=" %%F in ('dir /b /o-d "%USERPROFILE%\Downloads\client_secret_*.json" 2^>nul') do (
    if not exist client_secret.json copy /y "%USERPROFILE%\Downloads\%%F" client_secret.json >nul
  )
)
if not exist client_secret.json (
  echo client_secret.json not found in Downloads. Tell Claude.
  pause
  exit /b
)
echo client_secret.json OK
python -m pip install --quiet google-api-python-client google-auth-oauthlib
python -u yt_poster.py login
pause

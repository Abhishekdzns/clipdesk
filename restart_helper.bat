@echo off
cd /d "%~dp0"
timeout /t 4 /nobreak > NUL
python -u server.py >> clipdesk_log.txt 2>&1

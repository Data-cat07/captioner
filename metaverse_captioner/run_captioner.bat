@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_captioner.ps1"
pause

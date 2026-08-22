@echo off
setlocal
cd /d "%~dp0"
if "%PORT%"=="" set "PORT=8000"
python github_actions_fetcher.py
if errorlevel 1 pause
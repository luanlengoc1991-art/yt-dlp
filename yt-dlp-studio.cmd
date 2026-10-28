@echo off
title yt-dlp Studio
cd /d "%~dp0"
".venv\Scripts\python.exe" gui\server.py %*

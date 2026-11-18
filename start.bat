@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting PIVOK Post Constructor Bot...
"C:\Mix_Env\Python311\python.exe" app.py
pause

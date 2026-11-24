@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0SETUP_WINDOWS.ps1"
set "BASKET_EXIT_CODE=%errorlevel%"
if not "%BASKET_EXIT_CODE%"=="0" pause
exit /b %BASKET_EXIT_CODE%

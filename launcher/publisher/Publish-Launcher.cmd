@echo off
setlocal DisableDelayedExpansion
set "root=%~dp0.."
if "%~1"=="" goto usage
if "%~2"=="" goto usage
python -B -I "%root%\publish_online.py" publish-launcher --installer "%~1" --manifest "%~2"
set "result=%errorlevel%"
pause
exit /b %result%
:usage
echo Usage: Publish-Launcher.cmd UmaRU-Setup.exe launcher.json
echo Sign launcher.json before publishing. No files were uploaded.
pause
exit /b 2

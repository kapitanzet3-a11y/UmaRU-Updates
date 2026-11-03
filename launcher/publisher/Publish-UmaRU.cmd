@echo off
setlocal DisableDelayedExpansion
set "root=%~dp0.."
if "%~1"=="" goto choose
python -B -I "%root%\publish_online.py" release --published "%~1"
goto finish
:choose
python -B -I "%root%\publish_online.py" release
:finish
set "result=%errorlevel%"
pause
exit /b %result%

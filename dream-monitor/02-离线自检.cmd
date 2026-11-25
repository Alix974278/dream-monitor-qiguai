@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
".runtime\python.exe" -X utf8 -u check_local.py
set "dream_exit=%errorlevel%"
pause
popd
exit /b %dream_exit%

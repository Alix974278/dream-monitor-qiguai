@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
".runtime\python.exe" -X utf8 -u run_local.py --host 127.0.0.1
set "dream_exit=%errorlevel%"
if not "%dream_exit%"=="0" pause
popd
exit /b %dream_exit%

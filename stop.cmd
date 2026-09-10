@echo off
REM Stop everything the tutor cannot stop itself: the containers.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
call "%PY%" -m backend.tools.down %*
exit /b %ERRORLEVEL%

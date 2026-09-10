@echo off
REM Start everything: containers, wait for VOICEVOX, the tutor, the avatar page.
REM Windows has no `make`; this is the one command the README promises.
REM
REM `call` matters: without it, Ctrl+C inside python makes cmd.exe ask "Terminate batch
REM job (Y/N)?" and sit there until answered, which looks like the app failing to exit.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
call "%PY%" -m backend.tools.up %*
exit /b %ERRORLEVEL%

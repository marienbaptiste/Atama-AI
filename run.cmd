@echo off
REM Start everything: containers, wait for VOICEVOX, the tutor, the avatar page.
REM Windows has no `make`, and this is the one command the README promises.
setlocal
set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m backend.tools.up %*

@echo off
REM Start everything: containers, wait for VOICEVOX, build the page if its source changed,
REM the tutor, and open the page.
REM Windows has no `make`; this is the one command the README promises.
REM
REM `call` matters: without it, Ctrl+C inside python makes cmd.exe ask "Terminate batch
REM job (Y/N)?" and sit there until answered, which looks like the app failing to exit.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo no virtual environment at .venv - create it first:
    echo     python -m venv .venv ^&^& .venv\Scripts\pip install -e ".[dev]"
    echo ^(README: Setup^)
    exit /b 1
)
call "%PY%" -m backend.tools.up %*
exit /b %ERRORLEVEL%

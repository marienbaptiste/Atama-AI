@echo off
REM Stop everything the tutor cannot stop itself: the containers.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo no virtual environment at .venv - create it first:
    echo     python -m venv .venv ^&^& .venv\Scripts\pip install -e ".[dev]"
    echo ^(README: Setup^)
    exit /b 1
)
call "%PY%" -m backend.tools.down %*
exit /b %ERRORLEVEL%

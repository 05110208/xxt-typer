@echo off
rem xxt_typer launcher -- ASCII only + CRLF, because cmd.exe cannot parse UTF-8 batch files
chcp 65001 >nul
setlocal
set "DIR=%~dp0"
call "%DIR%_find_env.bat"
if not defined PY (
  echo [ERROR] conda environment "xxt-typer" was not found.
  echo         Please run install.bat first to create it.
  pause
  exit /b 1
)
if exist "%PYW%" (
  start "" "%PYW%" "%DIR%xxt_typer.py"
) else (
  start "" "%PY%" "%DIR%xxt_typer.py"
)
exit /b 0

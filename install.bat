@echo off
rem xxt_typer installer -- ASCII only + CRLF
chcp 65001 >nul
setlocal
set "DIR=%~dp0"
set "ENV_NAME=xxt-typer"

where conda >nul 2>nul
if errorlevel 1 (
  echo [ERROR] conda was not found in PATH.
  echo         Open "Anaconda Prompt" in this folder and run this script again.
  pause
  exit /b 1
)

conda env list | findstr /r /c:"^%ENV_NAME% " >nul 2>nul
if errorlevel 1 (
  echo [1/3] creating conda environment %ENV_NAME% ...
  call conda create -y -n %ENV_NAME% python=3.11 tk pip
  if errorlevel 1 (
    echo [ERROR] failed to create the environment.
    pause
    exit /b 1
  )
) else (
  echo [1/3] environment %ENV_NAME% already exists, skipping.
)

echo [2/3] installing pynput ...
call conda run -n %ENV_NAME% python -m pip install -r "%DIR%requirements.txt"
if errorlevel 1 (
  echo [ERROR] failed to install dependencies.
  pause
  exit /b 1
)

echo [3/3] running self test ...
call conda run -n %ENV_NAME% python "%DIR%selftest.py"

echo.
echo Done. Double-click run.bat to start the tool.
pause

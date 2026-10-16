@echo off
rem Locate python.exe / pythonw.exe of the conda environment "xxt-typer".
rem Usage:  call "%~dp0_find_env.bat"   ->  sets PY and PYW (empty when not found)
set "PY="
set "PYW="
if /i "%CONDA_DEFAULT_ENV%"=="xxt-typer" if exist "%CONDA_PREFIX%\python.exe" (
  set "PY=%CONDA_PREFIX%\python.exe"
  set "PYW=%CONDA_PREFIX%\pythonw.exe"
)
for %%B in (
  "%USERPROFILE%\anaconda3"
  "%USERPROFILE%\miniconda3"
  "%USERPROFILE%\Anaconda3"
  "%USERPROFILE%\Miniconda3"
  "C:\ProgramData\anaconda3"
  "C:\ProgramData\miniconda3"
  "C:\anaconda3"
  "C:\miniconda3"
  "D:\anaconda"
  "D:\anaconda3"
  "D:\miniconda3"
) do (
  if not defined PY if exist "%%~B\envs\xxt-typer\python.exe" (
    set "PY=%%~B\envs\xxt-typer\python.exe"
    set "PYW=%%~B\envs\xxt-typer\pythonw.exe"
  )
)
exit /b 0

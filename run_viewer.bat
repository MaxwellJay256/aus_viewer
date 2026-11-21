@echo off
setlocal
set "ENV_NAME=us-process"

where conda >nul 2>nul
if errorlevel 1 (
    echo [ERROR] conda not found. Install Miniconda or Anaconda and add it to PATH.
    pause
    exit /b 1
)

conda run -n "%ENV_NAME%" --no-capture-output python "%~dp0mview.py" %*
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to run the viewer. If the environment is missing, create it first:
    echo     conda create -n %ENV_NAME% python=3.12
    pause
)
endlocal

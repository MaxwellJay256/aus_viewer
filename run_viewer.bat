@echo off
chcp 65001 >nul
setlocal
set "PY=D:\Maxwe\miniconda3\envs\us-process\python.exe"
if not exist "%PY%" (
    echo 未找到 us-process 环境: %PY%
    echo 请先运行: conda create -n us-process python=3.12 -y
    pause
    exit /b 1
)
"%PY%" "%~dp0mview.py" %*
if errorlevel 1 pause
endlocal

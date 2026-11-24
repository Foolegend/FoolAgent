@echo off
chcp 65001 >nul
setlocal

rem 有 .venv 就用它，避免装进虚拟环境的依赖在双击时找不到。
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

rem 双击运行时（无参数）结束后暂停，方便看结果；带参数时直接返回。
if "%~1"=="" (
    "%PY%" "%~dp0run.py"
    echo.
    pause
) else (
    "%PY%" "%~dp0run.py" %*
)
exit /b %ERRORLEVEL%

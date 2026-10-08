@echo off
setlocal EnableExtensions

rem PID Lab one-click launcher. Keep the project path quoted because it may contain spaces or Chinese.
cd /d "%~dp0"
set "APP_ROOT=%~dp0"
set "PYTHON_EXE="
set "PYTHON_ARGS="

rem Reuse the project's virtual environment when it already exists.
if exist "%APP_ROOT%.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%APP_ROOT%.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=python"
    ) else (
        where py >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=py"
            set "PYTHON_ARGS=-3"
        )
    )
)

if not defined PYTHON_EXE goto :no_python

"%PYTHON_EXE%" %PYTHON_ARGS% --version >nul 2>&1
if errorlevel 1 goto :no_python

rem Install only when one of the runtime imports is missing.
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import PySide6, pyqtgraph, serial, numpy, control" >nul 2>&1
if errorlevel 1 (
    echo PID Lab runtime dependencies are missing. Installing from requirements.txt...
    "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install -r "%APP_ROOT%requirements.txt"
    if errorlevel 1 goto :install_failed
)

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import matlab.engine" >nul 2>&1
if errorlevel 1 (
    echo Note: MATLAB Engine for Python was not found. Local NumPy mode is still available.
) else (
    echo MATLAB Engine detected.
)

echo Starting PID Lab...
set "PYTHONUTF8=1"
"%PYTHON_EXE%" %PYTHON_ARGS% "%APP_ROOT%main.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" goto :app_failed
exit /b 0

:no_python
echo [ERROR] Python 3 was not found. Install Python 3.10+ and try again.
goto :pause_and_fail

:install_failed
echo [ERROR] Dependency installation failed. Check network access or run:
echo         pip install -r requirements.txt
goto :pause_and_fail

:app_failed
echo [ERROR] PID Lab exited with code %APP_EXIT_CODE%.

:pause_and_fail
pause
exit /b 1

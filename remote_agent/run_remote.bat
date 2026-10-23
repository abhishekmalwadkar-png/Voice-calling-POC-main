@echo off
REM =========================================================================
REM  Remote Machine Launcher
REM  Runs start_server.bat under log_streamer.py to broadcast output to portal.
REM =========================================================================
cd /d "%~dp0"

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python 3 is not found in PATH! Please install Python or add it to PATH.
    pause
    exit /b 1
)

echo.
echo =========================================================================
echo   Starting Monitored Server Streamer...
echo   Logs will appear here and stream live to your Local Portal!
echo =========================================================================
echo.

python log_streamer.py %*
pause

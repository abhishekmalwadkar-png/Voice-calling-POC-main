@echo off
REM =========================================================================
REM  Start Live Log Portal (Local Machine)
REM  Usage:
REM    start_portal.bat           (Starts on local network port 8088)
REM    start_portal.bat --tunnel  (Starts with public Cloudflare tunnel)
REM =========================================================================
cd /d "%~dp0"

echo.
echo =========================================================================
echo   Starting FLM Live Log Portal...
echo =========================================================================
echo.

REM Open browser after 1 second
start "" powershell -Command "Start-Sleep -Seconds 1; Start-Process 'http://localhost:8088'"

REM Start the portal server
python portal\portal_server.py %*
pause

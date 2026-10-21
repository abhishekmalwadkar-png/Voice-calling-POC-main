@echo off
REM Starts the FLM voice call platform on port 80 with a Cloudflare public address.
REM Run from an Administrator cmd if nginx has to be stopped.
cd /d "%~dp0"

REM Stop an older copy of this server (and its tunnel) so port 80 is free
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='cloudflared.exe'\" | Where-Object { $_.CommandLine -match 'app\.py|localhost:80$' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped old process ' + $_.ProcessId) }"

REM nginx (Windows service) also wants port 80 - stop it if it is running
sc query nginx | find "RUNNING" >nul && net stop nginx

echo.
echo Starting... the public URL is printed below as "Public base URL".
echo Keep this window open - closing it stops the server.
echo.
python app.py
pause

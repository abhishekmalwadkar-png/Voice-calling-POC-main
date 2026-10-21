@echo off
REM Starts the sample callback receiver on port 5058 with a Cloudflare public address.
REM Use the printed "Public callback URL" as callback_url in events (or RESULT_WEBHOOK_URL in .env).
cd /d "%~dp0"

REM Stop an older copy of the receiver (and its tunnel) so port 5058 is free
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='cloudflared.exe'\" | Where-Object { $_.CommandLine -match 'callback_receiver\.py|localhost:5058$' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped old process ' + $_.ProcessId) }"

echo.
echo Starting... the public URL is printed below as "Public callback URL".
echo Keep this window open - closing it stops the receiver.
echo.
python callback_receiver.py --tunnel
pause

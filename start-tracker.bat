@echo off
rem Starts the tracker and keeps it updated from GitHub. Double-click, or run at login with Task Scheduler.
rem Optional: set TRACKER_PASSWORD=your-password   (needed for other devices / Tailscale)
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 updater.py %*) else (python updater.py %*)

@echo off
rem F&O Scanner - run today's schedule by hand in a console window.
rem (The scheduled task runs pythonw.exe directly, with no window.)
rem Extra arguments are passed through, e.g.:  run_day.cmd --serve
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m scanner run-day %*
exit /b %ERRORLEVEL%

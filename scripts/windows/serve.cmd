@echo off
rem F&O Scanner - start the dashboard by hand in a console window (Ctrl+C to stop).
rem Extra arguments are passed through, e.g.:  serve.cmd --host 127.0.0.1
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m scanner serve %*
exit /b %ERRORLEVEL%

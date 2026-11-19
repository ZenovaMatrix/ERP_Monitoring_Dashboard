@echo off
REM Start the ERP Monitoring Dashboard (production mode). Double-click, or run from Task Scheduler.
cd /d "%~dp0"
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
:loop
python run_server.py
echo Dashboard stopped. Restarting in 10 seconds (close this window to stop)...
timeout /t 10 /nobreak >nul
goto loop

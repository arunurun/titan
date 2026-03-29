@echo off
REM Double-click is OK; Task Scheduler can run this directly.
REM Logs append to logs\titan_live.log — same folder as Breeze SDK may write under cwd.

setlocal
cd /d "%~dp0.."
if not exist "logs" mkdir logs

REM Use "py" if "python" is not on PATH (adjust if you use a venv).
python main.py --live >> logs\titan_live.log 2>&1
REM py main.py --live >> logs\titan_live.log 2>&1

endlocal

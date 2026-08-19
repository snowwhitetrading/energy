@echo off
REM Weekly Westwood offshore jackup rig scrape (driven by Windows Task Scheduler).
REM --all is used so a year rollover, a late correction or a missed week is
REM picked up automatically; every write is an idempotent upsert.
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"

echo. >> "%ROOT%logs\westwood_scraper.log"
"%ROOT%.venv\Scripts\python.exe" "%ROOT%westwood_scraper.py" --all --excel "%ROOT%ThiTruongKhoan.xlsx" >> "%ROOT%logs\westwood_scraper.log" 2>&1
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" echo [FAILED with exit code %RC%] >> "%ROOT%logs\westwood_scraper.log"
exit /b %RC%

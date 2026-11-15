@echo off
REM One-click weekly ADOT Bid Tabulation sync into Neon Postgres
REM Edit DATABASE_URL below OR create a .env.local file in the repo root.

cd /d "%~dp0\.."

if "%DATABASE_URL%"=="" (
    if not exist ".env.local" (
        echo ERROR: Set DATABASE_URL or create .env.local with DATABASE_URL=postgresql://...
        pause
        exit /b 1
    )
)

python -m pip install -q -r requirements.txt playwright
python -m playwright install chromium
python scripts\sync_adot_browser.py --limit 100 --save-dir data\adot_pdfs

echo.
echo Sync complete! Press any key to close.
pause

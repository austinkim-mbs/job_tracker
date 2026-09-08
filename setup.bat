@echo off
setlocal

echo === Job Application Tracker Setup ===
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org and add to PATH.
    pause
    exit /b 1
)

:: Install dependencies
echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

:: Copy .env.example if .env doesn't exist
if not exist .env (
    copy .env.example .env
    echo.
    echo Created .env from template.
    echo IMPORTANT: Edit .env and fill in your ANTHROPIC_API_KEY before running.
    echo.
) else (
    echo .env already exists, skipping.
)

:: Create credentials folder
if not exist credentials mkdir credentials
echo Place your google_oauth.json in the credentials\ folder.
echo.

echo === Setup complete ===
echo.
echo Next steps:
echo   1. Edit .env with your API keys
echo   2. Add credentials\google_oauth.json from Google Cloud Console
echo   3. Run: python poll_applications.py --once   (foreground / test)
echo   4. Run: python service.py install  then  python service.py start  (background service)
echo.
pause

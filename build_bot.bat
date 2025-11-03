@echo off
setlocal enabledelayedexpansion

REM Ensure Python is available
python --version >NUL 2>&1
if errorlevel 1 (
    echo Python is not installed or not added to PATH.
    pause
    exit /b 1
)

REM Create virtual environment if it does not exist
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 goto :error
)

call venv\Scripts\activate
if errorlevel 1 goto :error

python -m pip install --upgrade pip
if errorlevel 1 goto :error

pip install -r requirements.txt
if errorlevel 1 goto :error

pyinstaller --noconfirm --onefile --name datamatrix_bot ^
    --collect-all aspose_barcode_cloud ^
    --collect-all telegram ^
    bot_app.py
if errorlevel 1 goto :error

echo.
echo Build completed successfully. Executable available in dist\datamatrix_bot.exe
pause
exit /b 0

:error
echo.
echo Build process failed. See the log above for details.
pause
exit /b 1

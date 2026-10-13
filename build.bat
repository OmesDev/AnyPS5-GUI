@echo off
rem Build AnyPS5 Relinker into dist\AnyPS5Relinker\ (includes the bundled AnyPS5 engine)
cd /d "%~dp0"
python -m pip install -q -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onedir --windowed --name AnyPS5Relinker --add-data "ui;ui" --add-data "engine;engine" --add-data "patches;patches" main.py
if errorlevel 1 (echo Build failed & pause & exit /b 1)
echo.
echo Built: dist\AnyPS5Relinker\AnyPS5Relinker.exe
echo Zip the whole dist\AnyPS5Relinker folder to share it.
pause

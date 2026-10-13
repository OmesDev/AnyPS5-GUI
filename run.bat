@echo off
rem Launch AnyPS5 Relinker from source (no console window)
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" pythonw main.py

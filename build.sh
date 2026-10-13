#!/usr/bin/env bash
# Build AnyPS5 Relinker into dist/AnyPS5Relinker/ (includes the bundled AnyPS5 engine)
set -e
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt pyinstaller
python3 -m PyInstaller --noconfirm --clean --onedir --windowed --name AnyPS5Relinker --add-data "ui:ui" --add-data "engine:engine" --add-data "patches:patches" main.py
echo "Built: dist/AnyPS5Relinker/AnyPS5Relinker"

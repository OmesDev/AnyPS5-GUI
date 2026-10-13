#!/usr/bin/env bash
# Launch AnyPS5 Relinker from source
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
python3 main.py

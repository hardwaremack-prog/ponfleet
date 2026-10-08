#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -c "import flask" 2>/dev/null || sudo apt-get install -y python3-flask || pip install flask --break-system-packages -q
exec python3 fleet.py --open

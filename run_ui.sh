#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ ! -d venv ]; then bash setup.sh; fi
if grep -q 'your_client_id_here' .env 2>/dev/null; then
    echo '[ERROR] Please fill in your Amadeus API credentials in .env'
    exit 1
fi

echo '[*] Installing/updating dependencies...'
venv/bin/pip install -r requirements.txt -q

echo
echo '========================================='
echo '  Flight Price Monitor  –  Web UI'
echo '  http://localhost:5050'
echo '  Press Ctrl+C to stop'
echo '========================================='
echo

venv/bin/python app.py

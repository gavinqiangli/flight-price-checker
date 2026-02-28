#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Auto-setup if venv missing
if [ ! -d venv ]; then
    echo '[*] First run – running setup...'
    bash setup.sh
fi

# Check .env exists and has credentials filled in
if [ ! -f .env ]; then
    echo '[ERROR] .env not found. Run: bash setup.sh'
    exit 1
fi

if grep -q 'your_client_id_here' .env 2>/dev/null; then
    echo '[ERROR] Please fill in your Amadeus API credentials in .env'
    echo '        nano $DIR/.env'
    exit 1
fi

echo '[*] Starting Flight Price Checker (WSL)...'
venv/bin/python flight_checker.py

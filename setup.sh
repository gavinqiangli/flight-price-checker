#!/usr/bin/env bash
set -e
cd "."

echo '=== Flight Price Checker – WSL Setup ==='

# Create .env if missing
if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo '[!] .env created from template.'
    echo '    Fill in your Amadeus API credentials before running:'
    echo '    nano .env'
    echo
fi

# Create virtual environment
if [ ! -d venv ]; then
    echo '[*] Creating Python virtual environment...'
    python3 -m venv venv
fi

# Install dependencies
echo '[*] Installing dependencies...'
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

echo
echo '[OK] Setup complete!'
echo
echo 'Next steps:'
echo '  1. Edit .env with your Amadeus credentials:  nano .env'
echo '  2. Run the checker:  ./run.sh'

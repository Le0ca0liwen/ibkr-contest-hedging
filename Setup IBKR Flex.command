#!/bin/zsh
cd "${0:A:h}"
.venv/bin/python ops/setup_ibkr_flex.py
read -r '?Press Enter to close.'

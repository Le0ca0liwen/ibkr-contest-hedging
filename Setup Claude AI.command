#!/bin/zsh
cd "${0:A:h}"
.venv/bin/python ops/setup_claude_ai.py
read -r '?Press Enter to close.'

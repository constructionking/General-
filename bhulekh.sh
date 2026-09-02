#!/bin/zsh
# convenience wrapper: ./bhulekh.sh scan -d Amroha
cd "$(dirname "$0")" && exec .venv/bin/python -m bhulekh "$@"

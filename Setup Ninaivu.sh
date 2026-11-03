#!/bin/sh
# One-time Linux app setup. --apps-only refreshes launchers after moving it.
set -eu
cd "$(dirname "$0")" || exit 1
if [ "${1:-}" != --apps-only ]; then
    sh launcher/start.sh --setup-only
fi
if command -v cc >/dev/null 2>&1; then
    cc -O2 -o 'Open Ninaivu' installers/linux/portable/open-ninaivu.c
    chmod +x 'Open Ninaivu'
fi
python3 ninaivu/desktop/linux_menu.py --add --root "$PWD"
printf '\nNinaivu is ready. Open Ninaivu from the application menu.\n'
if ! .venv/bin/python -c 'import tkinter' 2>/dev/null; then
    printf 'The Control Panel needs Tk. On Linux Mint/Ubuntu/Debian: sudo apt install python3-tk\n'
fi

#!/bin/sh
# Ninaivu on macOS and Linux: set up and start. Passes its arguments on.
cd "$(dirname "$0")/.." || exit 1
exec python3 launcher/start.py "$@"

#!/bin/sh
# Ninaivu on macOS and Linux: set up and start. Passes its arguments on.
#
# It runs on the first Python 3.12 or newer it finds. On a Mac without one,
# launcher/install-python-mac.sh offers to install Python 3.12 first; on Linux
# the distribution's package manager is the place for that, and start.py says
# which version it needs.
cd "$(dirname "$0")/.." || exit 1
PYTHON="$(sh launcher/install-python-mac.sh --find)"
if [ -z "$PYTHON" ] && [ "$(uname -s)" = Darwin ]; then
    sh launcher/install-python-mac.sh && PYTHON="$(sh launcher/install-python-mac.sh --find)"
fi
exec "${PYTHON:-python3}" launcher/start.py "$@"

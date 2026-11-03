#!/bin/sh
# Ninaivu on macOS and Linux: set up and start. Passes its arguments on.
#
#     sh launcher/start.sh --add-to-menu    # Linux: one click from the menu and Desktop
#
# It runs on the first Python 3.12 or newer it finds. On a Mac without one,
# launcher/install-python-mac.sh offers to install Python 3.12 first; on Linux
# the distribution's package manager is the place for that, and start.py says
# which version it needs.
cd "$(dirname "$0")/.." || exit 1
# On Linux, once: Ninaivu and the Ninaivu Control Panel in the application
# menu and on the Desktop, opened with one click from then on
# (ninaivu/desktop/linux_menu.py). --remove-from-menu takes them out.
case "${1:-}" in
    --add-to-menu) exec python3 ninaivu/desktop/linux_menu.py --add --root "$PWD" ;;
    --remove-from-menu) exec python3 ninaivu/desktop/linux_menu.py --remove ;;
esac
PYTHON="$(sh launcher/install-python-mac.sh --find)"
if [ -z "$PYTHON" ] && [ "$(uname -s)" = Darwin ]; then
    sh launcher/install-python-mac.sh && PYTHON="$(sh launcher/install-python-mac.sh --find)"
fi
exec "${PYTHON:-python3}" launcher/start.py "$@"

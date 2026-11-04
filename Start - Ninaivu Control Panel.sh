#!/bin/sh
# Ninaivu Control Panel on Linux - double-click to open it, as "Start -
# Ninaivu Control Panel.vbs" does on Windows. Linux Mint's file manager asks
# whether to run it; choose Run. (GNOME Files opens scripts as text instead:
# there, `sh launcher/start.sh --add-to-menu` puts the Control Panel in the
# application menu and on the Desktop, and that entry runs this file.)
#
# It runs ninaivu_control.pyw with the Python in this folder's .venv, which
# `sh launcher/start.sh` creates the first time it runs. Without one it says
# so, in a window when there is no terminal to say it in.
cd "$(dirname "$0")" || exit 1

[ ! -f launcher/linux-env.sh ] || . ./launcher/linux-env.sh

TITLE="Ninaivu Control Panel"

tell() {
    echo "$TITLE: $1" >&2
    # Somebody who double-clicked has no terminal to read that in.
    # NINAIVU_NO_DIALOG for a script or a test.
    [ -n "${NINAIVU_NO_DIALOG:-}" ] && return 0
    [ -t 2 ] && return 0
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="$TITLE" --no-markup --text="$1" 2>/dev/null && return 0
    fi
    if command -v kdialog >/dev/null 2>&1; then
        kdialog --title "$TITLE" --error "$1" 2>/dev/null && return 0
    fi
    if command -v notify-send >/dev/null 2>&1; then
        notify-send "$TITLE" "$1" 2>/dev/null || true
    fi
}

PYTHON="$PWD/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    tell "Ninaivu is not set up in this folder yet, so there is no Python environment for the Control Panel.

Run this once in a terminal, in $PWD:

    sh launcher/start.sh

then open the Control Panel again."
    exit 1
fi

if [ "${1:-}" = --autostart ]; then
    shift
    exec "$PYTHON" -m ninaivu.desktop.autostart --start "$@"
fi
exec "$PYTHON" "$PWD/ninaivu_control.pyw" "$@"

#!/bin/bash
# ---------------------------------------------------------------------------
#  Ninaivu — double-click launcher for macOS.
#
#  Double-click this file in Finder. It sets everything up the first time and
#  starts the library; after that it just starts it.
#
#  If macOS refuses to open this file, you have not run "Setup Ninaivu" yet —
#  do that once and this becomes an ordinary double-click, along with a
#  Ninaivu icon and the Ninaivu Control Panel in your Applications folder.
#
#  Everything it installs lives in a .venv folder beside this file, apart from
#  Python itself when the Mac has none new enough. Deleting the Ninaivu folder
#  removes the rest.
# ---------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
# The folder Ninaivu keeps its index and settings in, where the server
# looks for it too; the remembered library goes in there, and so into its
# backups.
SETTINGS="${NINAIVU_STATE_DIR:-$HOME/.ninaivu}"
REMEMBERED="$SETTINGS/library-path"

# Terminal opens at whatever size it likes; make the banner readable.
printf '\033[8;34;100t' 2>/dev/null || true

say()  { printf '\n  \033[36m•\033[0m %s\n' "$1"; }
warn() { printf '\n  \033[33m!\033[0m %s\n' "$1"; }
fail() { printf '\n  \033[31m✗\033[0m %s\n\n' "$1"; }

tell_user() {
    osascript -e "display dialog \"$1\" buttons {\"OK\"} default button \"OK\" \
        with title \"Ninaivu\" with icon $2" >/dev/null 2>&1
}

pause_before_closing() {
    printf '\n  Press Return to close this window.\n'
    read -r _ || true
}

# --- 1. Python -------------------------------------------------------------
#
# Ninaivu needs Python 3.12 or newer. The .venv's, once the setup has made
# one; otherwise the first one new enough on this Mac. When there is none,
# launcher/install-python-mac.sh asks, installs it (with Homebrew, or the
# signed installer from python.org), and it is looked for again.

new_enough() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1
}

PYTHON=""
if [ -x "$HERE/.venv/bin/python" ] && new_enough "$HERE/.venv/bin/python"; then
    PYTHON="$HERE/.venv/bin/python"
fi
[ -n "$PYTHON" ] || PYTHON="$(sh "$HERE/launcher/install-python-mac.sh" --find)"

if [ -z "$PYTHON" ]; then
    warn "Ninaivu needs Python 3.12 or newer, and this Mac does not have it yet."
    if sh "$HERE/launcher/install-python-mac.sh"; then
        PYTHON="$(sh "$HERE/launcher/install-python-mac.sh" --find)"
    fi
    if [ -z "$PYTHON" ]; then
        fail "No Python 3.12 or newer, so Ninaivu cannot start."
        printf '      Install it from https://www.python.org/downloads/macos/ and\n'
        printf '      double-click Ninaivu again.\n'
        pause_before_closing
        exit 1
    fi
fi

say "Using $("$PYTHON" --version 2>&1) at $PYTHON"

# --- 2. Which folder? ------------------------------------------------------
#
# Asked once, with the native folder chooser rather than by making somebody
# type a path — and remembered, so this is the only question it ever asks.

LIBRARY=""
# Only a real path counts as the folder. Without this test, running
# `Ninaivu.command --rescan` took "--rescan" for the library name, remembered
# it, and then failed on every run afterwards.
case "${1-}" in
    ""|-*) ;;
    *) LIBRARY="$1"; shift ;;
esac

if [ -z "$LIBRARY" ] && [ -f "$REMEMBERED" ]; then
    LIBRARY="$(cat "$REMEMBERED")"
    if [ ! -d "$LIBRARY" ]; then
        warn "The folder from last time is not there any more:"
        printf '      %s\n' "$LIBRARY"
        LIBRARY=""
    fi
fi

if [ -n "$LIBRARY" ] && [ ! -d "$LIBRARY" ]; then
    fail "That is not a folder: $LIBRARY"
    pause_before_closing
    exit 1
fi

if [ -z "$LIBRARY" ]; then
    say "Choose the folder holding your photos and videos…"
    LIBRARY=$(osascript -e 'POSIX path of (choose folder with prompt "Which folder holds your photos and videos?")' 2>/dev/null)
    if [ -z "$LIBRARY" ]; then
        fail "No folder chosen, so there is nothing to open."
        pause_before_closing
        exit 1
    fi
    LIBRARY="${LIBRARY%/}"
fi

case "$LIBRARY" in
    *.photoslibrary|*.photoslibrary/*)
        tell_user "That is Apple's Photos library, which is a sealed package rather than a folder — Photos rearranges the inside of it whenever it likes.\n\nExport the photos you want into an ordinary folder and point Ninaivu at that instead." caution
        fail "Refusing to index a .photoslibrary package."
        rm -f "$REMEMBERED"
        pause_before_closing
        exit 1
        ;;
esac

# Only ever remember somewhere that exists, so a typo cannot poison the next
# twenty launches.
if [ -d "$LIBRARY" ]; then
    mkdir -p "$SETTINGS"
    printf '%s' "$LIBRARY" > "$REMEMBERED"
fi
say "Library: $LIBRARY"

# --- 3. Set up and run -----------------------------------------------------
#
# launcher/start.py does the real work on every platform: it makes the .venv,
# installs what is missing, picks free ports and starts both apps. This file
# exists to get somebody to that point without opening a terminal.

printf '\n'
"$PYTHON" launcher/start.py "$LIBRARY" "$@"
STATUS=$?

if [ $STATUS -ne 0 ]; then
    fail "Ninaivu stopped with an error (exit code $STATUS)."
    printf '      The lines above say why. The usual causes are a folder that\n'
    printf '      cannot be read, or no network connection during first setup.\n'
    pause_before_closing
fi
exit $STATUS

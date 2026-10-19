#!/bin/sh
# Ninaivu.app/Contents/MacOS/ninaivu: what opening Ninaivu runs.
#
# Opens the Ninaivu Control Panel (--tray: the menu-bar tray instead) on the
# app's own Python, in one of two ways:
#
#  * Portable, when the app sits in the folder of the portable zip — a
#    `.ninaivu-portable` file beside Ninaivu.app. Everything Ninaivu keeps is
#    in that folder: data/ (the index, settings, accounts; the Control
#    Panel's own files in data/control), data/ai-models, and logs/. Nothing
#    outside the folder is written, so it can be moved, or carried on a
#    drive, as one thing.
#  * Otherwise (the app dragged on its own into Applications), as an app:
#    the Control Panel's files in ~/Library/Application Support/Ninaivu and
#    the index in ~/.ninaivu.
#
# Copied in by installers/macos/build.sh; Ninaivu.app is signed with it.
here=$(cd "$(dirname "$0")/.." && pwd -P)          # Ninaivu.app/Contents
bundle=$(dirname "$here")                           # Ninaivu.app
folder=$(dirname "$bundle")                         # what Ninaivu.app is in
export NINAIVU_PYTHON="$here/Resources/python/bin/python3"
unset PYTHONHOME PYTHONPATH
# The app's modules were compiled when it was built: a cache written into
# Ninaivu.app at run time would change a signed app (build.sh, 3b).
export PYTHONDONTWRITEBYTECODE=1

say() {
    echo "Ninaivu: $1" >&2
    # A window to read it in, for someone who opened the app from Finder;
    # NINAIVU_NO_DIALOG for a script or a test, where nobody can close one.
    [ -n "${NINAIVU_NO_DIALOG:-}" ] && return 0
    /usr/bin/osascript -e 'on run argv' -e 'display alert "Ninaivu" message (item 1 of argv) as critical' \
        -e 'end run' "$1" >/dev/null 2>&1 || true
}

case "$bundle" in
    */AppTranslocation/*)
        # macOS ran the app from a temporary, read-only copy (App
        # Translocation: a downloaded app opened where it was unpacked), so
        # the folder beside it cannot be seen. Run that way, the portable
        # copy would quietly start an empty Ninaivu somewhere else. The
        # Security framework knows where the copy came from: when that is a
        # portable folder, its quarantine is cleared — the app has already
        # been allowed to open — and the app is opened again from there.
        original=$("$NINAIVU_PYTHON" "$here/Resources/original_path.py" "$bundle" 2>/dev/null || true)
        if [ -n "$original" ] && [ -d "$original" ] \
                && [ -f "$(dirname "$original")/.ninaivu-portable" ]; then
            /usr/bin/xattr -dr com.apple.quarantine "$(dirname "$original")" 2>/dev/null || true
            # Only once the quarantine is really gone: opened again with it
            # still there, macOS would translocate it again, and round we go.
            if ! /usr/bin/xattr -p com.apple.quarantine "$original" >/dev/null 2>&1; then
                /usr/bin/open -n "$original" --args "$@" && exit 0
            fi
        fi
        say "macOS opened Ninaivu from a temporary copy, so it cannot see its own folder. Open Terminal and run:

xattr -dr com.apple.quarantine <the Ninaivu folder>

(type the first part, then drag the folder onto the Terminal window), and open Ninaivu again. README-PORTABLE.txt in the folder says more."
        exit 1 ;;
esac

if [ -f "$folder/.ninaivu-portable" ]; then
    if ! mkdir -p "$folder/data/control" "$folder/data/ai-models" "$folder/logs" 2>/dev/null \
            || [ ! -w "$folder/data" ]; then
        say "Ninaivu cannot write to the folder it is in ($folder). Move the whole folder somewhere you can write to, such as your Documents, and open it there."
        exit 1
    fi
    export NINAIVU_HOME="$folder/data/control"
    export NINAIVU_STATE_DIR="$folder/data"
    export NINAIVU_AI_MODELS_DIR="$folder/data/ai-models"
    export NINAIVU_LOG_DIR="$folder/logs"
    export NINAIVU_PORTABLE=1
    export NINAIVU_PORTABLE_ROOT="$folder"
    export NINAIVU_LAUNCHER="$here/MacOS/ninaivu"
else
    export NINAIVU_HOME="$HOME/Library/Application Support/Ninaivu"
    mkdir -p "$NINAIVU_HOME"
fi

case "${1:-}" in
    --where)
        printf 'NINAIVU_HOME=%s\nNINAIVU_STATE_DIR=%s\nNINAIVU_AI_MODELS_DIR=%s\nNINAIVU_LOG_DIR=%s\nNINAIVU_PYTHON=%s\n' \
            "$NINAIVU_HOME" "${NINAIVU_STATE_DIR:-}" "${NINAIVU_AI_MODELS_DIR:-}" "${NINAIVU_LOG_DIR:-}" "$NINAIVU_PYTHON"
        exit 0 ;;
    --tray) shift; exec "$NINAIVU_PYTHON" -m ninaivu.desktop.tray "$@" ;;
    # From Terminal, what the Control Panel offers after an update:
    # Ninaivu.app/Contents/MacOS/ninaivu --bring <older folder> | --go-back
    --bring|--go-back) exec "$NINAIVU_PYTHON" -m ninaivu.desktop.portable "$@" ;;
esac
exec "$NINAIVU_PYTHON" -m ninaivu.desktop.app "$@"

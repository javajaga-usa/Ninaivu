#!/bin/bash
# ---------------------------------------------------------------------------
#  Ninaivu — one-time setup for macOS, from a checkout or a downloaded folder.
#
#  Run this once. It installs everything Ninaivu needs and puts two apps in
#  your Applications folder: Ninaivu, which starts the library in a Terminal
#  window, and the Ninaivu Control Panel, which starts and stops it without
#  one. After that both are ordinary double-clicks, and you never come back
#  here.
#
#  Why this file exists at all, rather than just an app you double-click:
#  anything that arrives on a Mac from the internet is marked "quarantined",
#  and recent versions of macOS will not run a quarantined app that is not
#  signed by a registered developer — the old right-click-and-Open trick was
#  removed. An app *created on this Mac* carries no such mark. So this script
#  builds the launchers locally, and they open with one click forever.
#
#  Everything lives in a .venv folder beside this file; deleting the Ninaivu
#  folder removes all of it. The one exception is Python itself: Ninaivu needs
#  3.12 or newer, and when this Mac has none it is installed (after asking)
#  by launcher/install-python-mac.sh — with Homebrew, or the signed installer
#  from python.org.
#
#      "./Setup Ninaivu.command" --apps-only    # rebuild the two apps, nothing else
# ---------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
APPS_DIR="$HOME/Applications"
# The main Applications folder, where the Control Panel is copied too when
# this account may write there. Overridable so the tests never touch it.
SYSTEM_APPS_DIR="${NINAIVU_SYSTEM_APPS_DIR:-/Applications}"

# --apps-only rebuilds the two apps and nothing else: after the Ninaivu folder
# has moved, or on a Mac set up before the Control Panel had an app of its own.
APPS_ONLY=0
[ "${1:-}" = "--apps-only" ] && APPS_ONLY=1

printf '\033[8;40;100t' 2>/dev/null || true

step()  { printf '\n  \033[1;36m%s\033[0m %s\n' "$1" "$2"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$1"; }
fail()  { printf '\n  \033[31m✗\033[0m %s\n\n' "$1"; }
note()  { printf '    \033[2m%s\033[0m\n' "$1"; }

finish() {
    printf '\n  Press Return to close this window.\n'
    read -r _ || true
}

printf '\n  \033[1;36mNinaivu\033[0m — setting up on %s\n' "$(sw_vers -productName 2>/dev/null || echo macOS) $(sw_vers -productVersion 2>/dev/null)"

# --- 1. Take the quarantine mark off this folder ---------------------------
#
# This script is running, so the user has already got past Gatekeeper once for
# it. Clearing the flag on the rest of the folder means the apps built below,
# and every file they use, are treated as local from here on.

step "1" "Clearing the download quarantine"
if xattr -dr com.apple.quarantine "$HERE" 2>/dev/null; then
    ok "Done — macOS will stop treating these files as a download."
else
    note "Nothing to clear, which is fine."
fi

# --- 2. Python -------------------------------------------------------------
#
# 3.12 or newer. Apple's command line tools bring 3.9, which Ninaivu refuses,
# so they are no help here; the helper installs Python 3.12 instead, after
# asking. Not needed to rebuild the apps.

step "2" "Looking for Python 3.12 or newer"
PYTHON=""
if [ "$APPS_ONLY" = 1 ]; then
    note "Skipped — only the apps are being rebuilt."
else
    PYTHON="$(sh "$HERE/launcher/install-python-mac.sh" --find)"
    if [ -z "$PYTHON" ]; then
        warn "This Mac has no Python 3.12 or newer."
        note "Ninaivu installs Python 3.12 once: with Homebrew when this Mac has it,"
        note "otherwise the installer signed by the Python Software Foundation."
        printf '\n'
        if sh "$HERE/launcher/install-python-mac.sh"; then
            PYTHON="$(sh "$HERE/launcher/install-python-mac.sh" --find)"
        fi
        if [ -z "$PYTHON" ]; then
            fail "No Python 3.12 or newer, so the setup cannot go on. Nothing else was changed."
            note "Install it from https://www.python.org/downloads/macos/ and run Setup Ninaivu again."
            finish
            exit 1
        fi
    fi
    ok "$("$PYTHON" --version 2>&1) — $PYTHON"
fi

# --- 3. Dependencies -------------------------------------------------------

step "3" "Installing what Ninaivu needs"
if [ "$APPS_ONLY" = 1 ]; then
    note "Skipped — only the apps are being rebuilt."
else
    note "First time only. It is a few dozen megabytes and takes a minute or two."
    printf '\n'
    if ! "$PYTHON" launcher/start.py --setup-only; then
        fail "Setup did not finish."
        note "The lines above say why. The usual cause is no internet connection."
        finish
        exit 1
    fi
    # The Control Panel is drawn with Tk. python.org's Python has it;
    # Homebrew's has it only with python-tk, which the helper adds when it
    # installed Python itself, but not to a Homebrew Python that was here.
    if ! "$HERE/.venv/bin/python" -c 'import tkinter' >/dev/null 2>&1; then
        warn "This Python has no Tk, so the Control Panel will not open yet."
        note "With Homebrew: brew install python-tk@3.12. Ninaivu itself, the tray and"
        note "the console work without it."
    fi
fi

# --- 4. Build the launchers, here, so macOS never questions them -----------

step "4" "Building the Ninaivu apps"

# Ninaivu's own icon, from the same iconset the installer's app is built with.
ICON_WORK="$(mktemp -d)"
ICNS="$ICON_WORK/Ninaivu.icns"
if command -v iconutil >/dev/null 2>&1 \
   && iconutil -c icns "$HERE/installers/macos/ninaivu.iconset" -o "$ICNS" 2>/dev/null; then
    ok "Icon built"
else
    note "No icon — harmless, the apps will use the default."
fi

# make_app NAME IDENTIFIER — an app bundle in ~/Applications with its
# Info.plist and icon. Whoever calls it writes Contents/MacOS/NAME.
make_app() {
    local APP_NAME="$1" BUNDLE_ID="$2"
    local APP="$APPS_DIR/$APP_NAME.app"
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>$APP_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
    [ -f "$ICNS" ] && cp "$ICNS" "$APP/Contents/Resources/$APP_NAME.icns"
}

# Tell Finder about an app, so its icon appears now rather than on reboot.
register_app() {
    touch "$1"
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "$1" >/dev/null 2>&1 || true
}

mkdir -p "$APPS_DIR"

# The folder's path, as one single-quoted shell word for the launchers below.
# Written into them bare, a folder named with a " or a $ or a backtick broke
# the launcher, or ran what followed it as a command, as M-05 did elsewhere.
Q_HERE="'$(printf '%s' "$HERE" | sed "s/'/'\\\\''/g")'"

# Ninaivu itself opens the launcher in Terminal rather than running it
# silently: a server with no window gives you nothing to read and no way to
# stop it. (The identifiers are not the installer's org.ninaivu.app, so the
# two never pass for each other.)
APP="$APPS_DIR/Ninaivu.app"
make_app "Ninaivu" "local.ninaivu.launcher"
cat > "$APP/Contents/MacOS/Ninaivu" <<LAUNCHER
#!/bin/bash
here=$Q_HERE
open -a Terminal "\$here/Ninaivu.command"
LAUNCHER
chmod +x "$APP/Contents/MacOS/Ninaivu"
chmod +x "$HERE/Ninaivu.command" 2>/dev/null || true
register_app "$APP"
ok "Ninaivu is in your Applications folder"

# The Control Panel is the other way to run it, as on Windows: a window of its
# own with no Terminal — Start, Stop, Restart, the resource mode and the logs.
# A server it starts runs in the background and outlives the panel.
PANEL="$APPS_DIR/Ninaivu Control Panel.app"
make_app "Ninaivu Control Panel" "local.ninaivu.control"
cat > "$PANEL/Contents/MacOS/Ninaivu Control Panel" <<LAUNCHER
#!/bin/bash
here=$Q_HERE
cd "\$here" || exit 1
if [ ! -x "\$here/.venv/bin/python" ]; then
    osascript -e 'display dialog "Ninaivu is not set up on this Mac yet. Run Setup Ninaivu first." buttons {"OK"} default button "OK" with title "Ninaivu Control Panel" with icon caution' >/dev/null 2>&1
    exit 1
fi
exec "\$here/.venv/bin/python" "\$here/ninaivu_control.pyw"
LAUNCHER
chmod +x "$PANEL/Contents/MacOS/Ninaivu Control Panel"
register_app "$PANEL"
ok "Ninaivu Control Panel is in your Applications folder"

# And the same app in two more places: beside this file, where Windows keeps
# the Control Panel's launcher, and in the main Applications folder with the
# other apps. Copies, made again on every run, so a moved Ninaivu folder does
# not leave one pointing at where it used to be.
copy_app() {
    local FROM="$1" TO_DIR="$2"
    [ -d "$TO_DIR" ] && [ -w "$TO_DIR" ] || return 1
    rm -rf "${TO_DIR:?}/$(basename "$FROM")"
    cp -R "$FROM" "$TO_DIR/" || return 1
    register_app "$TO_DIR/$(basename "$FROM")"
}
copy_app "$PANEL" "$HERE" && ok "Ninaivu Control Panel is in the Ninaivu folder too"
if copy_app "$PANEL" "$SYSTEM_APPS_DIR"; then
    ok "Ninaivu Control Panel is in $SYSTEM_APPS_DIR too"
else
    note "Not copied to $SYSTEM_APPS_DIR: this account cannot write there. The one in your own Applications folder is the same app."
fi

rm -rf "$ICON_WORK" 2>/dev/null || true

if [ "$APPS_ONLY" = 1 ]; then
    printf '\n  \033[1;32mDone.\033[0m Both apps are in %s.\n\n' "$APPS_DIR"
    exit 0
fi

# --- 5. Done ---------------------------------------------------------------

printf '\n  \033[1;32mSet up.\033[0m\n\n'
note "Open Ninaivu from Applications, Launchpad or Spotlight."
note "The Ninaivu Control Panel starts and stops it without a Terminal window."
note "The first time, Ninaivu asks which folder holds your photos, and remembers."
printf '\n'

answer=$(osascript -e 'display dialog "Ninaivu is set up and is now in your Applications folder, with the Ninaivu Control Panel beside it.\n\nOpen it from Launchpad or Spotlight whenever you want the library running.\n\nStart it now?" buttons {"Later", "Start Ninaivu"} default button "Start Ninaivu" with title "Ninaivu" with icon note' -e 'button returned of result' 2>/dev/null)

if [ "$answer" = "Start Ninaivu" ]; then
    open "$APP"
    exit 0
fi

open "$APPS_DIR" 2>/dev/null || true
finish

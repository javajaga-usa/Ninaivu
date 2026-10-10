#!/bin/sh
# Ninaivu - find, or install, a Python new enough for it on a Mac.
#
#   sh launcher/install-python-mac.sh --find   # prints the first Python 3.12+, or fails
#   sh launcher/install-python-mac.sh          # asks, then installs Python 3.12
#
# Shared by Ninaivu.command, "Setup Ninaivu.command" and launcher/start.sh, so
# the three look in the same places and install the same way.
#
# Ninaivu needs Python 3.12 or newer (MIN_PYTHON in launcher/start.py). The
# python3 that Apple's command line tools bring is 3.9, so asking macOS for
# those - what the older launchers did - installs a Python that is then
# refused. Instead, after asking:
#   1. Homebrew, when the Mac has it: `brew install python@3.12`, no password.
#      Homebrew's Python has no Tk, which the Control Panel is drawn with, so
#      python-tk@3.12 is added too; the rest of Ninaivu works without it.
#   2. Otherwise the installer from python.org. It is only run once macOS
#      confirms it is signed by the Python Software Foundation with a Developer
#      ID that Apple issued, so a download that was tampered with or cut short
#      is refused rather than run. Installing it needs an administrator's
#      password, which macOS asks for in its own window.
#
# Exit code 0 means an installer finished; the caller then looks for Python
# again (with --find), so it is the finding, not this script, that decides
# whether it worked. For the tests: NINAIVU_NO_BREW=1 leaves Homebrew out, and
# NINAIVU_PYTHON_CERTIFICATES names another "Install Certificates" to run.

VERSION=3.12.10
PKG_URL="https://www.python.org/ftp/python/$VERSION/python-$VERSION-macos11.pkg"

say()  { printf '  %s\n' "$1"; }

# Accepts a candidate only if it runs and is 3.12 or newer.
new_enough() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1
}

# The first Python 3.12+: on PATH by name, then where python.org's installer
# and Homebrew put one - the last so a Python installed a moment ago is found
# before PATH catches up with it.
find_python() {
    for name in python3.14 python3.13 python3.12 python3; do
        found=$(command -v "$name" 2>/dev/null) || continue
        if new_enough "$found"; then
            printf '%s\n' "$found"
            return 0
        fi
    done
    for version in 3.14 3.13 3.12; do
        for found in "/Library/Frameworks/Python.framework/Versions/$version/bin/python3" \
                     "/opt/homebrew/bin/python$version" "/usr/local/bin/python$version"; do
            if [ -x "$found" ] && new_enough "$found"; then
                printf '%s\n' "$found"
                return 0
            fi
        done
    done
    return 1
}

find_brew() {
    [ "${NINAIVU_NO_BREW:-}" = 1 ] && return 1
    for found in "$(command -v brew 2>/dev/null)" /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [ -n "$found" ] && [ -x "$found" ]; then
            printf '%s\n' "$found"
            return 0
        fi
    done
    return 1
}

# A question in a dialog, because a Terminal window nobody has looked at yet
# is not where a question belongs. Prints the button chosen; nothing when the
# dialog was cancelled or could not be shown.
ask() {
    osascript -e "display dialog \"$1\" buttons {\"Not now\", \"$2\"} default button \"$2\" with title \"Ninaivu\" with icon note" \
              -e 'button returned of result' 2>/dev/null
}

install_with_brew() {
    say "Installing Python 3.12 with Homebrew..."
    if ! "$1" install python@3.12; then
        say "Homebrew could not install Python 3.12."
        return 1
    fi
    say "Adding Tk for the Control Panel (python-tk@3.12)..."
    "$1" install python-tk@3.12 || say "python-tk@3.12 did not install; the Control Panel needs it, the rest of Ninaivu does not."
    return 0
}

install_from_python_org() {
    work=$(mktemp -d "${TMPDIR:-/tmp}/ninaivu-python.XXXXXX") || return 1
    pkg="$work/python-$VERSION-macos11.pkg"
    say "Downloading Python $VERSION from python.org..."
    if ! curl -fL --retry 2 -o "$pkg" "$PKG_URL"; then
        say "The download did not finish. Check the internet connection and try again."
        rm -rf "$work"
        return 1
    fi

    # Both lines, or it is not run: signed by the PSF, and with a certificate
    # Apple issued rather than one anybody could make. Each is matched as a
    # whole line: the signer is the first certificate of the chain, the PSF's
    # own Developer ID with its team id, not any certificate whose name
    # merely contains "Python Software Foundation".
    signature=$(pkgutil --check-signature "$pkg" 2>&1)
    signed_by_psf=0
    if printf '%s\n' "$signature" \
            | grep -Eq '^[[:space:]]*1\. Developer ID Installer: Python Software Foundation \(BMM5U3QVKW\)[[:space:]]*$'; then
        signed_by_psf=1
    fi
    issued_by_apple=0
    if printf '%s\n' "$signature" \
            | grep -Eq '^[[:space:]]*Status: signed by a developer certificate issued by Apple( |$)'; then
        issued_by_apple=1
    fi
    if [ "$signed_by_psf" != 1 ] || [ "$issued_by_apple" != 1 ]; then
        say "The downloaded installer is not signed by the Python Software Foundation,"
        say "so it was not run. Nothing was installed. What macOS said about it:"
        printf '%s\n' "$signature" | sed 's/^/      /'
        say "Install Python 3.12 yourself from https://www.python.org/downloads/macos/"
        rm -rf "$work"
        return 1
    fi

    say "Installing Python $VERSION. macOS asks for your password in its own window."
    if ! osascript -e 'on run argv' \
                   -e 'do shell script "installer -pkg " & quoted form of (item 1 of argv) & " -target /" with administrator privileges' \
                   -e 'end run' "$pkg" >/dev/null; then
        say "The installer did not run (cancelled, or no administrator password)."
        rm -rf "$work"
        return 1
    fi
    rm -rf "$work"

    # python.org's Python brings no root certificates of its own until this
    # is run; without them its urllib cannot check an HTTPS server, and
    # Ninaivu's cloud copies, webhook and AI model downloads would fail.
    # (Ninaivu looks nothing up on its own; nothing here is an update check.)
    # Harmless to skip.
    certificates="${NINAIVU_PYTHON_CERTIFICATES:-/Applications/Python 3.12/Install Certificates.command}"
    if [ -x "$certificates" ]; then
        "$certificates" >/dev/null 2>&1 || say "(Install Certificates did not finish; run it from /Applications/Python 3.12 if downloads fail.)"
    fi
    return 0
}

if [ "${1:-}" = "--find" ]; then
    find_python
    exit $?
fi

brew=$(find_brew)
if [ -n "$brew" ]; then
    how="It will be installed with Homebrew, which is already on this Mac."
else
    how="It comes from python.org, and macOS will ask for your password to install it."
fi
answer=$(ask "Ninaivu needs Python 3.12 or newer, and this Mac does not have it yet.\n\n$how It takes a few minutes, once.\n\nInstall Python 3.12 now?" "Install")
if [ "$answer" != "Install" ]; then
    say "Python was not installed. Nothing was changed."
    exit 1
fi

if [ -n "$brew" ]; then
    install_with_brew "$brew"
else
    install_from_python_org
fi

#!/usr/bin/env bash
# Build Ninaivu.app and a .dmg, signed and notarised when the identity is set.
#
#   sh installers/macos/build.sh            from the repository root
#
# The app is a plain bundle: a private, relocatable Python (a
# python-build-standalone "install_only" build — a venv would not do: it keeps
# the standard library in the Python it was made from, and links that
# Python's framework by an absolute path, so it runs only on the build
# machine) with Ninaivu and its wheels installed into it, an Info.plist that keeps it off
# the Dock (it is a menu-bar tray), and a launcher that sets NINAIVU_HOME to
# Application Support and opens the tray. Nothing is downloaded when it runs.
#
# Signing and notarising happen when these are set (the release workflow
# sets them from secrets); otherwise the build is unsigned and says so, and
# Gatekeeper will refuse it on another Mac until it is allowed in System
# Settings. A release is signed only when the repository has the secrets; its
# notes say which.
#
#   NINAIVU_MAC_SIGN_IDENTITY    "Developer ID Application: Name (TEAMID)"
#   NINAIVU_NOTARY_PROFILE       a `xcrun notarytool store-credentials` profile
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
version=$(python3 -c "import re,pathlib;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', pathlib.Path('$root/ninaivu/__init__.py').read_text()).group(1))")
arch=$(uname -m)
echo "Ninaivu $version ($arch)"

build="$here/build"
app="$build/Ninaivu.app"
rm -rf "$build"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

# 1. A private, relocatable Python with everything installed.
#    PBS_PYTHON and PBS_RELEASE pick the build
#    (https://github.com/astral-sh/python-build-standalone/releases).
pbs_python=${PBS_PYTHON:-3.12.11}
pbs_release=${PBS_RELEASE:-20250708}
case "$arch" in
    arm64) triple=aarch64-apple-darwin ;;
    x86_64) triple=x86_64-apple-darwin ;;
    *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
esac
tarball="cpython-${pbs_python}+${pbs_release}-${triple}-install_only.tar.gz"
# GitHub's release downloads now and then answer 500 for a moment; try again
# rather than fail the build. The digest check below still decides.
curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o "$build/$tarball" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/${pbs_release}/${tarball}"
# Checked against the digest pinned in the repository, not only HTTPS: the
# release could be replaced, and every installer would carry what it held.
sums="$root/installers/python-build-standalone.sha256"
want=$(awk -v name="$tarball" '$2 == name { print $1 }' "$sums")
if [ -z "$want" ]; then
    echo "no pinned SHA-256 for $tarball in $sums; add it from the release's SHA256SUMS" >&2
    exit 1
fi
got=$(shasum -a 256 "$build/$tarball" | cut -d' ' -f1)
if [ "$got" != "$want" ]; then
    echo "$tarball does not match its pinned SHA-256 (got $got)" >&2
    rm -f "$build/$tarball"
    exit 1
fi
tar -xzf "$build/$tarball" -C "$app/Contents/Resources"      # unpacks to ./python
rm "$build/$tarball"
py="$app/Contents/Resources/python/bin/python3"
"$py" -m pip install --quiet --upgrade -c "$root/requirements/build-tools.txt" pip wheel
# Ninaivu and the extensions as wheels, with the source compiled away
# (installers/strip_sources.py), then installed from those wheels.
wheels="$build/wheels"
mkdir -p "$wheels"
"$py" -m pip wheel --quiet --wheel-dir "$wheels" --no-deps "$root" "$root/extensions/gemini" "$root/extensions/creative-studio"
"$py" "$root/installers/strip_sources.py" "$wheels"/ninaivu*.whl
# The versions the tests ran with (constraints-tested.txt), not whatever the
# index serves on the day. Fetched as wheels first and installed from those
# files only, so the list below names, by SHA-256, exactly what went in.
deps="$build/deps"
rm -rf "$deps"
mkdir -p "$deps"
"$py" -m pip wheel --quiet --wheel-dir "$deps" \
    -r "$root/requirements/requirements.txt" -r "$root/requirements/requirements-desktop.txt" \
    -c "$root/requirements/constraints-tested.txt"
"$py" -m pip install --quiet --no-index --find-links "$deps" \
    -r "$root/requirements/requirements.txt" -r "$root/requirements/requirements-desktop.txt" \
    -c "$root/requirements/constraints-tested.txt"
"$py" -m pip install --quiet --no-deps --no-index --find-links "$wheels" ninaivu ninaivu-gemini ninaivu-creative-studio
# What went in, each wheel with its SHA-256, as the Windows and Linux builds
# list theirs: attached to the release beside the disk image. A `pip freeze`
# here named versions only, while the release notes said "with hashes".
(cd "$deps" && shasum -a 256 -- *.whl; cd "$wheels" && shasum -a 256 -- *.whl) \
    | sort -k2 > "$build/Ninaivu-$version-macos-$arch-packages.txt"
rm -rf "$wheels" "$deps"
find "$app/Contents/Resources/python" -name "__pycache__" -type d -prune -exec rm -rf {} +

# 2. The launcher: the Control Panel, with its files under Application
#    Support. The tray is beside it, for `open -a Ninaivu --args --tray` or
#    the panel's own menu.
cat > "$app/Contents/MacOS/ninaivu" <<'LAUNCH'
#!/bin/sh
here=$(cd "$(dirname "$0")/.." && pwd)
export NINAIVU_HOME="$HOME/Library/Application Support/Ninaivu"
export NINAIVU_PYTHON="$here/Resources/python/bin/python3"
mkdir -p "$NINAIVU_HOME"
case "$1" in
    --tray) shift; exec "$NINAIVU_PYTHON" -m ninaivu.desktop.tray "$@" ;;
esac
exec "$NINAIVU_PYTHON" -m ninaivu.desktop.app "$@"
LAUNCH
chmod +x "$app/Contents/MacOS/ninaivu"
"$py" -c "import tkinter" || { echo "the bundled Python has no Tk; the Control Panel needs it" >&2; exit 1; }

# 3. The icon and the plist.
iconutil -c icns "$here/ninaivu.iconset" -o "$app/Contents/Resources/ninaivu.icns"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Ninaivu</string>
  <key>CFBundleDisplayName</key><string>Ninaivu</string>
  <key>CFBundleIdentifier</key><string>org.ninaivu.app</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>ninaivu</string>
  <key>CFBundleIconFile</key><string>ninaivu</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><false/>
  <key>NSHumanReadableCopyright</key><string>Jagadeesh Rajendran · MIT licence</string>
  <key>NSLocalNetworkUsageDescription</key><string>Ninaivu answers to phones and tablets on the home network.</string>
</dict></plist>
PLIST

# 4. Sign — every Mach-O inside, then the bundle — and notarise.
if [ -n "${NINAIVU_MAC_SIGN_IDENTITY:-}" ]; then
    # Every Mach-O inside first — a failure here is a failed release, not
    # something to hide: notarisation would refuse the app anyway.
    find "$app/Contents/Resources/python" -type f \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) \
        -print0 | while IFS= read -r -d '' file; do
            if file -b "$file" | grep -q Mach-O; then
                codesign --force --options runtime --timestamp --sign "$NINAIVU_MAC_SIGN_IDENTITY" \
                    --entitlements "$here/entitlements.plist" "$file"
            fi
        done
    codesign --force --deep --options runtime --timestamp --sign "$NINAIVU_MAC_SIGN_IDENTITY" \
        --entitlements "$here/entitlements.plist" "$app"
    codesign --verify --deep --strict "$app"
else
    echo "warning: unsigned app; set NINAIVU_MAC_SIGN_IDENTITY for a release" >&2
fi

dmg="$build/Ninaivu-$version-macos-$arch.dmg"
staging="$build/dmg"
mkdir -p "$staging"
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"
# Read before dragging a new version over the old one: a server still running
# from the old app would keep running from files replaced under it.
cat > "$staging/Before updating.txt" <<'NOTE'
Updating Ninaivu

1. Stop Ninaivu first: open the Ninaivu Control Panel, press Stop and wait
   until it says Stopped. Then quit the Control Panel.
2. Drag the new Ninaivu onto Applications and choose Replace.
3. Open Ninaivu again and press Start.

Only the program is replaced. Your photographs, settings, index, people and
AI models are kept as they are: they live outside the app, in ~/.ninaivu,
~/Library/Application Support/Ninaivu and your library folder. The first
start of the new version copies the settings and the index into
~/.ninaivu/backups/before-update before it changes anything.

Ninaivu never looks on the internet for a newer version.
NOTE
# hdiutil fails now and then with "Resource busy" on GitHub's macOS runners —
# the disk image it mounts to fill is still being released by the system
# (Spotlight, or the previous image) when it asks for it again — and both
# architectures failed that way on one otherwise green run. It is not the
# staging folder: nothing of ours holds it. A short wait and another go is
# what works; five tries covers the longest release seen.
for attempt in 1 2 3 4 5; do
    if hdiutil create -volname "Ninaivu" -srcfolder "$staging" -ov -format UDZO "$dmg" >/dev/null; then
        break
    fi
    if [ "$attempt" -eq 5 ]; then
        echo "error: hdiutil could not create $dmg after $attempt tries" >&2
        exit 1
    fi
    echo "hdiutil did not finish (try $attempt); waiting before trying again" >&2
    sleep $((attempt * 5))
done
rm -rf "$staging"

if [ -n "${NINAIVU_MAC_SIGN_IDENTITY:-}" ] && [ -n "${NINAIVU_NOTARY_PROFILE:-}" ]; then
    codesign --force --timestamp --sign "$NINAIVU_MAC_SIGN_IDENTITY" "$dmg"
    xcrun notarytool submit "$dmg" --keychain-profile "$NINAIVU_NOTARY_PROFILE" \
        ${NINAIVU_NOTARY_KEYCHAIN:+--keychain "$NINAIVU_NOTARY_KEYCHAIN"} --wait
    xcrun stapler staple "$dmg"
    xcrun stapler staple "$app"
fi

hash=$(shasum -a 256 "$dmg" | cut -d' ' -f1)
echo "$dmg"
echo "SHA256 $hash"

# The Homebrew cask for this version, from the template.
mkdir -p "$build/homebrew"
sed -e "s/__VERSION__/$version/g" -e "s/__SHA256_$(echo "$arch" | tr a-z A-Z)__/$hash/g" \
    "$here/homebrew/ninaivu.rb" > "$build/homebrew/ninaivu.rb"
echo "cask in $build/homebrew/ninaivu.rb (fill the other architecture's hash from its build)"

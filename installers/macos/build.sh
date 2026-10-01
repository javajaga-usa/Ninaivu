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
# Gatekeeper will refuse it on another Mac — which is why releases are signed.
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
curl -fsSL -o "$build/$tarball" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/${pbs_release}/${tarball}"
tar -xzf "$build/$tarball" -C "$app/Contents/Resources"      # unpacks to ./python
rm "$build/$tarball"
py="$app/Contents/Resources/python/bin/python3"
"$py" -m pip install --quiet --upgrade pip wheel
# Ninaivu and the extensions as wheels, with the source compiled away
# (installers/strip_sources.py), then installed from those wheels.
wheels="$build/wheels"
mkdir -p "$wheels"
"$py" -m pip wheel --quiet --wheel-dir "$wheels" --no-deps "$root" "$root/extensions/gemini" "$root/extensions/creative-studio"
"$py" "$root/installers/strip_sources.py" "$wheels"/ninaivu*.whl
"$py" -m pip install --quiet -r "$root/requirements/requirements.txt" -r "$root/requirements/requirements-desktop.txt"
"$py" -m pip install --quiet --no-deps --no-index --find-links "$wheels" ninaivu ninaivu-gemini ninaivu-creative-studio
rm -rf "$wheels"
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
hdiutil create -volname "Ninaivu" -srcfolder "$staging" -ov -format UDZO "$dmg" >/dev/null
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

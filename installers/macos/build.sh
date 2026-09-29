#!/bin/sh
# Build Ninaivu.app and a .dmg, signed and notarised when the identity is set.
#
#   sh installers/macos/build.sh            from the repository root
#
# The app is a plain bundle: a private Python (python.org's framework build,
# whichever `python3` this script runs with, copied by `venv --copies`) with
# Ninaivu and its wheels installed into it, an Info.plist that keeps it off
# the Dock (it is a menu-bar tray), and a launcher that sets NINAIVU_ROOT to
# Application Support and opens the tray. Nothing is downloaded when it runs.
#
# Signing and notarising happen when these are set (the release workflow
# sets them from secrets); otherwise the build is unsigned and says so, and
# Gatekeeper will refuse it on another Mac — which is why releases are signed.
#
#   NINAIVU_MAC_SIGN_IDENTITY    "Developer ID Application: Name (TEAMID)"
#   NINAIVU_NOTARY_PROFILE       a `xcrun notarytool store-credentials` profile
set -eu

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
python3 -m venv --copies "$app/Contents/Resources/python"
py="$app/Contents/Resources/python/bin/python3"
"$py" -m pip install --quiet --upgrade pip wheel
"$py" -m pip install --quiet "$root" -r "$root/requirements/requirements-desktop.txt"
"$py" -m pip install --quiet --no-deps "$root/extensions/gemini" "$root/extensions/creative-studio"
# A venv records where it was made; the app is opened from elsewhere.
sed -i '' "s|^home = .*|home = /Applications/Ninaivu.app/Contents/Resources/python/bin|" \
    "$app/Contents/Resources/python/pyvenv.cfg"
find "$app/Contents/Resources/python" -name "__pycache__" -type d -prune -exec rm -rf {} +

# 2. The launcher: the tray, with its files under Application Support.
cat > "$app/Contents/MacOS/ninaivu" <<'LAUNCH'
#!/bin/sh
here=$(cd "$(dirname "$0")/.." && pwd)
export NINAIVU_ROOT="$HOME/Library/Application Support/Ninaivu"
export NINAIVU_PYTHON="$here/Resources/python/bin/python3"
mkdir -p "$NINAIVU_ROOT"
exec "$NINAIVU_PYTHON" -m ninaivu.desktop.tray "$@"
LAUNCH
chmod +x "$app/Contents/MacOS/ninaivu"

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
  <key>LSUIElement</key><true/>
  <key>NSHumanReadableCopyright</key><string>MIT licence</string>
  <key>NSLocalNetworkUsageDescription</key><string>Ninaivu answers to phones and tablets on the home network.</string>
</dict></plist>
PLIST

# 4. Sign — every Mach-O inside, then the bundle — and notarise.
if [ -n "${NINAIVU_MAC_SIGN_IDENTITY:-}" ]; then
    find "$app/Contents/Resources/python" \( -name "*.so" -o -name "*.dylib" -o -perm +111 -type f \) \
        -exec codesign --force --options runtime --timestamp --sign "$NINAIVU_MAC_SIGN_IDENTITY" \
        --entitlements "$here/entitlements.plist" {} \; 2>/dev/null || true
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
    xcrun notarytool submit "$dmg" --keychain-profile "$NINAIVU_NOTARY_PROFILE" --wait
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

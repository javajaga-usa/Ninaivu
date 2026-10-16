#!/usr/bin/env bash
# Build Ninaivu for the Mac, portable: one folder, in a .zip, for Apple silicon.
#
#   bash installers/macos/build.sh            on an Apple-silicon Mac
#
# Makes installers/macos/build/Ninaivu-<version>-macos-arm64-portable.zip,
# which unpacks to one folder holding Ninaivu.app, a README and a hidden
# `.ninaivu-portable` file. The app is a plain bundle: a private, relocatable
# Python (a python-build-standalone "install_only" build — a venv would not
# do: it keeps the standard library in the Python it was made from, and links
# that Python's framework by an absolute path, so it runs only on the build
# machine) with Ninaivu and its wheels installed into it, and a launcher
# (launcher.sh) that opens the Control Panel. Beside `.ninaivu-portable` the
# launcher keeps everything in the folder (data/, logs/); dragged on its own
# into Applications, the app keeps its files in Application Support, as an
# installed app does. Nothing is downloaded when it runs.
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
    *) echo "build this on an Apple-silicon Mac (this one is $arch)" >&2; exit 1 ;;
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
    | sort -k2 > "$build/Ninaivu-$version-macos-$arch-portable-packages.txt"
rm -rf "$wheels" "$deps"
find "$app/Contents/Resources/python" -name "__pycache__" -type d -prune -exec rm -rf {} +

# 2. The launcher: the Control Panel, with its files in the portable folder,
#    or under Application Support when the app is on its own (launcher.sh).
#    The tray is beside it, for `--tray` or the panel's own menu.
install -m 0755 "$here/launcher.sh" "$app/Contents/MacOS/ninaivu"
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

# 5. The portable folder, zipped. ditto keeps what a Mac app needs and a
#    plain zip loses: the bundle's symbolic links, its permissions and the
#    signature's extended attributes.
name="Ninaivu-$version-macos-$arch-portable"
stage="$build/$name"
rm -rf "$stage"
mkdir -p "$stage"
ditto "$app" "$stage/Ninaivu.app"
cp "$here/portable/README-PORTABLE.txt" "$stage/README-PORTABLE.txt"
printf 'This folder is a portable Ninaivu: Ninaivu.app keeps its data here.\n' > "$stage/.ninaivu-portable"
top=$(cd "$stage" && ls -A | sort | tr '\n' ',')
if [ "$top" != ".ninaivu-portable,Ninaivu.app,README-PORTABLE.txt," ]; then
    echo "the portable folder holds '$top' at its top" >&2
    exit 1
fi
"$stage/Ninaivu.app/Contents/MacOS/ninaivu" --where | grep -q "NINAIVU_STATE_DIR=$stage/data" \
    || { echo "the launcher does not see the portable folder" >&2; exit 1; }
rm -rf "$stage/data" "$stage/logs"                  # made by --where; made again on first open
zip="$build/$name.zip"
rm -f "$zip"
(cd "$build" && ditto -c -k --sequesterRsrc --keepParent "$name" "$zip")

if [ -n "${NINAIVU_MAC_SIGN_IDENTITY:-}" ] && [ -n "${NINAIVU_NOTARY_PROFILE:-}" ]; then
    # Notarised as the zip (a zip cannot be signed or stapled itself); the
    # ticket is stapled to the app and the zip made again around it, so it
    # opens without asking Apple when it is offline.
    xcrun notarytool submit "$zip" --keychain-profile "$NINAIVU_NOTARY_PROFILE" \
        ${NINAIVU_NOTARY_KEYCHAIN:+--keychain "$NINAIVU_NOTARY_KEYCHAIN"} --wait
    xcrun stapler staple "$stage/Ninaivu.app"
    rm -f "$zip"
    (cd "$build" && ditto -c -k --sequesterRsrc --keepParent "$name" "$zip")
fi
rm -rf "$stage"

hash=$(shasum -a 256 "$zip" | cut -d' ' -f1)
echo "$zip"
echo "SHA256 $hash"

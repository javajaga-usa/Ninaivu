#!/usr/bin/env bash
# Build the Linux / Raspberry Pi installer: one self-contained file per architecture.
#
#   bash installers/linux/build.sh amd64      # a PC
#   bash installers/linux/build.sh arm64      # Raspberry Pi 4 or 5 (64-bit OS), other arm64 boards
#
# Makes installers/linux/build/Ninaivu-<version>-linux-<arch>.sh: a shell
# script with a tarball on its end. Inside: a private, relocatable Python (a
# python-build-standalone "install_only" build, so the machine needs no Python
# of its own), every wheel Ninaivu needs for that architecture, Ninaivu and
# its two extensions as bytecode-only wheels (installers/strip_sources.py),
# and install.sh, which puts it all under one folder, makes a `ninaivu`
# command, a desktop entry and a systemd service, and starts it. Nothing is
# downloaded when it is run.
#
# Either architecture builds on any Linux machine: the wheels are fetched for
# the target with pip's --platform, and nothing here is executed from the
# target's Python.
set -euo pipefail

arch=${1:-}
case "$arch" in
    amd64) triple=x86_64-unknown-linux-gnu; platforms=(manylinux_2_28_x86_64 manylinux_2_17_x86_64 manylinux2014_x86_64 manylinux_2_5_x86_64 manylinux1_x86_64) ;;
    arm64) triple=aarch64-unknown-linux-gnu; platforms=(manylinux_2_28_aarch64 manylinux_2_17_aarch64 manylinux2014_aarch64) ;;
    *) echo "usage: $0 amd64|arm64" >&2; exit 2 ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
version=$(python3 -c "import re,pathlib;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', pathlib.Path('$root/ninaivu/__init__.py').read_text()).group(1))")
echo "Ninaivu $version for linux-$arch"

build="$here/build"
payload="$build/payload-$arch"
rm -rf "$payload"
mkdir -p "$payload/wheels" "$build"

# 1. The Python. PBS_PYTHON and PBS_RELEASE pick the build, as the Mac build does;
#    the bytecode below is compiled by a Python of the same minor version.
pbs_python=${PBS_PYTHON:-3.12.11}
pbs_release=${PBS_RELEASE:-20250708}
tarball="cpython-${pbs_python}+${pbs_release}-${triple}-install_only.tar.gz"
if [ ! -f "$build/$tarball" ]; then
    curl -fsSL -o "$build/$tarball" \
        "https://github.com/astral-sh/python-build-standalone/releases/download/${pbs_release}/${tarball}"
fi
tar -xzf "$build/$tarball" -C "$payload"           # unpacks to ./python
rm -rf "$payload/python/lib/python3.12/test" "$payload/python/lib/python3.12/idlelib"
find "$payload/python" -name "__pycache__" -type d -prune -exec rm -rf {} +

# 2. The wheels, for the target. Pure-Python ones are the same for every machine;
#    the compiled ones (numpy, Pillow, OpenCV, …) are fetched for the target's
#    platform. The core must be there; a recommended package without a wheel for
#    this architecture is left out and said so, as `pip install` would leave it.
platform_args=()
for p in "${platforms[@]}"; do platform_args+=(--platform "$p"); done
fetch() {   # fetch REQUIREMENT → 0 if a wheel came, 1 if none exists for the target
    python3 -m pip download --quiet --dest "$payload/wheels" --only-binary=:all: \
        --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
        "${platform_args[@]}" "$1" 2>/dev/null
}
core=(flask pillow numpy waitress psutil)
missing=()
while IFS= read -r line; do
    req=${line%%#*}; req=$(echo "$req" | xargs)
    [ -z "$req" ] && continue
    case "$req" in -r*|--*) continue ;; esac
    case "$req" in *win32*|*darwin*) continue ;; esac        # another platform's package
    if ! fetch "$req"; then
        name=$(echo "$req" | sed -E 's/[<>=!;~ ].*//')
        for c in "${core[@]}"; do [ "$c" = "$name" ] && { echo "no $name wheel for $arch" >&2; exit 1; }; done
        missing+=("$name")
    fi
done < <(cat "$root/requirements/requirements.txt" "$root/requirements/requirements-desktop.txt")
[ "${#missing[@]}" -gt 0 ] && echo "left out (no $arch wheel): ${missing[*]}" || true

# 3. Ninaivu and the extensions, bytecode only.
python3 -m pip wheel --quiet --wheel-dir "$payload/wheels" --no-deps "$root" "$root/extensions/gemini" "$root/extensions/creative-studio"
python3 "$root/installers/strip_sources.py" "$payload/wheels"/ninaivu*.whl

# 4. What install.sh needs, and the installer itself.
cp "$here/install.sh" "$root/LICENSE" "$root/README.md" "$payload/"
printf '%s\n' "$version" > "$payload/VERSION"
printf '%s\n' "$arch" > "$payload/ARCH"
out="$build/Ninaivu-$version-linux-$arch.sh"
{
    sed -e "s/__VERSION__/$version/g" -e "s/__ARCH__/$arch/g" "$here/header.sh"
    tar -C "$payload" -czf - .
} > "$out"
chmod +x "$out"
rm -rf "$payload"
hash=$(sha256sum "$out" | cut -d' ' -f1)
echo "$out"
echo "SHA256 $hash"

#!/usr/bin/env bash
# Build Ninaivu for Linux, portable: one folder, in a .tar.gz, for a 64-bit PC.
#
#   bash installers/linux/build.sh
#
# Makes installers/linux/build/Ninaivu-<version>-linux-x64-portable.tar.gz,
# which unpacks to one folder holding three things and nothing else:
#
#   ninaivu     opens the Control Panel (installers/linux/portable/ninaivu)
#   app/        a private, relocatable Python (a python-build-standalone
#               "install_only" build, so the machine needs no Python of its
#               own) with every package Ninaivu needs installed into it, and
#               Ninaivu and its two extensions as bytecode only
#               (installers/strip_sources.py); app/data and app/ai-models are
#               made there on first run
#   logs/       what Ninaivu writes about itself
#
# Nothing is installed and nothing is downloaded when it runs. Run on an
# x86-64 Linux machine: the packages are installed by the bundled Python.
set -euo pipefail

triple=x86_64-unknown-linux-gnu
platforms=(manylinux_2_28_x86_64 manylinux_2_17_x86_64 manylinux2014_x86_64 manylinux_2_5_x86_64 manylinux1_x86_64)
if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
    echo "build this on x86-64 Linux: the bundled Python installs the packages" >&2
    exit 1
fi

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
version=$(python3 -c "import re,pathlib;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', pathlib.Path('$root/ninaivu/__init__.py').read_text()).group(1))")
echo "Ninaivu $version for linux-x64, portable"

build="$here/build"
name="Ninaivu-$version-linux-x64-portable"
payload="$build/payload"
stage="$build/$name"
rm -rf "$stage"
rm -rf "$payload"
mkdir -p "$payload/wheels" "$build"

# 1. The Python. PBS_PYTHON and PBS_RELEASE pick the build, as the Mac build does;
#    the bytecode below is compiled by a Python of the same minor version.
pbs_python=${PBS_PYTHON:-3.12.11}
pbs_release=${PBS_RELEASE:-20250708}
# Ninaivu's bytecode (step 3) is compiled by this machine's python3, and a
# .pyc loads only on the minor version that made it: a build run with 3.13
# made an installer whose Python 3.12 could not import Ninaivu at all.
host_python=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if [ "$host_python" != "${pbs_python%.*}" ]; then
    echo "python3 here is $host_python; the bundled Python is ${pbs_python%.*}, and the bytecode must be made by the same" >&2
    exit 1
fi
tarball="cpython-${pbs_python}+${pbs_release}-${triple}-install_only.tar.gz"
if [ ! -f "$build/$tarball" ]; then
    # GitHub's release downloads now and then answer 500 for a moment; try again
    # rather than fail the build. The digest check below still decides.
    curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o "$build/$tarball" \
        "https://github.com/astral-sh/python-build-standalone/releases/download/${pbs_release}/${tarball}"
fi
# Checked against the digest pinned in the repository, not only HTTPS: the
# release could be replaced, and every installer would carry what it held.
sums="$root/installers/python-build-standalone.sha256"
want=$(awk -v name="$tarball" '$2 == name { print $1 }' "$sums")
if [ -z "$want" ]; then
    echo "no pinned SHA-256 for $tarball in $sums; add it from the release's SHA256SUMS" >&2
    exit 1
fi
got=$(sha256sum "$build/$tarball" | cut -d' ' -f1)
if [ "$got" != "$want" ]; then
    echo "$tarball does not match its pinned SHA-256 (got $got)" >&2
    rm -f "$build/$tarball"
    exit 1
fi
tar -xzf "$build/$tarball" -C "$payload"           # unpacks to ./python
rm -rf "$payload/python/lib/python3.12/test" "$payload/python/lib/python3.12/idlelib"
find "$payload/python" -name "__pycache__" -type d -prune -exec rm -rf {} +

# 2. The wheels, for the target. Pure-Python ones are the same for every machine;
#    the compiled ones (numpy, Pillow, OpenCV, …) are fetched for the target's
#    platform. The core must be there; a recommended package without a wheel for
#    this architecture is left out and said so, as `pip install` would leave it.
#    Every version is the one in constraints-tested.txt, the set the tests ran
#    with, not whatever the index serves on the day.
platform_args=()
for p in "${platforms[@]}"; do platform_args+=(--platform "$p"); done
fetch() {   # fetch REQUIREMENT → 0 if a wheel came, 1 if none exists for the target
    python3 -m pip download --quiet --dest "$payload/wheels" --only-binary=:all: \
        --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
        -c "$root/requirements/constraints-tested.txt" "${platform_args[@]}" "$1" 2>/dev/null
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
        for c in "${core[@]}"; do [ "$c" = "$name" ] && { echo "no $name wheel for linux-x64" >&2; exit 1; }; done
        missing+=("$name")
    fi
done < <(cat "$root/requirements/requirements.txt" "$root/requirements/requirements-desktop.txt")
[ "${#missing[@]}" -gt 0 ] && echo "left out (no linux-x64 wheel): ${missing[*]}" || true

# 3. Ninaivu and the extensions, bytecode only.
python3 -m pip wheel --quiet --wheel-dir "$payload/wheels" --no-deps "$root" "$root/extensions/gemini" "$root/extensions/creative-studio"
python3 "$root/installers/strip_sources.py" "$payload/wheels"/ninaivu*.whl

# What went in, with each wheel's SHA-256: attached to the release beside
# the archive, so what a release shipped is on record.
packages="$build/$name-packages.txt"
(cd "$payload/wheels" && sha256sum -- *.whl) > "$packages"
echo "$packages"

# 4. The folder: the Python with everything installed into it, offline, and
#    the launcher beside it.
mkdir -p "$stage/app" "$stage/logs"
mv "$payload/python" "$stage/app/python"
py="$stage/app/python/bin/python3"
"$py" -m pip install --quiet --no-index --no-deps --no-warn-script-location "$payload"/wheels/*.whl
"$py" -c "import ninaivu, tkinter" || { echo "the portable Python cannot import Ninaivu, or has no Tk for the Control Panel" >&2; exit 1; }
find "$stage/app/python" -name "__pycache__" -type d -prune -exec rm -rf {} +
# pip's own scripts name the build folder in their first line; nothing here
# runs them (the launcher runs python3 -m), and they would point nowhere.
find "$stage/app/python/bin" -maxdepth 1 -type f ! -name "python3*" -exec grep -l "^#!$stage" {} + 2>/dev/null \
    | xargs -r rm -f
install -m 0755 "$here/portable/ninaivu" "$stage/ninaivu"
cp "$root/LICENSE" "$stage/app/LICENSE.txt"
cp "$root/README.md" "$stage/app/README.md"
cp "$here/portable/README-PORTABLE.txt" "$stage/app/README-PORTABLE.txt"
printf '%s\n' "$version" > "$stage/app/VERSION"
top=$(cd "$stage" && ls -A | sort | tr '\n' ',')
if [ "$top" != "app,logs,ninaivu," ]; then
    echo "the portable folder holds '$top' at its top, not app, logs and ninaivu" >&2
    exit 1
fi
"$stage/ninaivu" --where >/dev/null

out="$build/$name.tar.gz"
tar -C "$build" -czf "$out" "$name"
rm -rf "$payload" "$stage"
hash=$(sha256sum "$out" | cut -d' ' -f1)
echo "$out"
echo "SHA256 $hash"

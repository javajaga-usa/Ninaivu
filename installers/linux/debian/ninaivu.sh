#!/bin/sh
# The Debian package owns /opt/ninaivu; every mutable file belongs to the user.
set -eu
app=$(cd "$(dirname "$0")" && pwd -P)
data=${NINAIVU_STATE_DIR:-$HOME/.ninaivu}
unset PYTHONHOME PYTHONPATH NINAIVU_PORTABLE NINAIVU_PORTABLE_ROOT
export PYTHONNOUSERSITE=1
mkdir -p "$data/control" "$data/ai-models" "$data/logs"
# Extras install into a user-owned venv. Core dependencies remain in the
# package's private runtime, and are replaced by apt when the package updates.
#
# A venv shows bin/python3 long before it is finished (pip is set up, and
# system site-packages switched on, last), so its presence says nothing. A
# marker written after the runtime is checked does; two first starts take
# turns under a lock, and a runtime left half-made by an interrupted start is
# made again (audit F2). Remade in place: a venv cannot be moved, its paths
# are written into it.
runtime="$data/runtime"
base="$app/python/bin/python3"
marker="$runtime/.ninaivu-ready"
lock="$data/runtime.lock"
minor=$("$base" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
stamp="$minor $app"

runtime_marked() {
    [ -x "$runtime/bin/python3" ] &&
    [ "$(cat "$marker" 2>/dev/null)" = "$stamp" ] &&
    grep -q '^include-system-site-packages = true' "$runtime/pyvenv.cfg" 2>/dev/null &&
    [ -d "$runtime/lib/python$minor/site-packages/pip" ]
}

runtime_works() {
    grep -q '^include-system-site-packages = true' "$runtime/pyvenv.cfg" 2>/dev/null || return 1
    needs=pip
    if [ -d "$app/python/lib/python$minor/site-packages/ninaivu" ]; then
        needs="pip, ninaivu"
    fi
    "$runtime/bin/python3" -c "import sys; assert sys.version_info[:2] == tuple(map(int, '$minor'.split('.'))); import $needs" >/dev/null 2>&1
}

release_lock() {
    rm -rf "$lock"
}

take_lock() {
    waited=0
    while ! mkdir "$lock" 2>/dev/null; do
        owner=$(cat "$lock/pid" 2>/dev/null || true)
        if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
            rm -rf "$lock"          # its start was killed; nobody holds it
            continue
        fi
        if [ "$waited" -ge 900 ]; then
            echo "ninaivu: another start is still preparing $runtime; try again shortly" >&2
            exit 75
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo $$ > "$lock/pid"
    trap release_lock EXIT
    trap 'release_lock; exit 130' INT TERM HUP
}

if ! runtime_marked; then
    take_lock
    # Another start may have finished it while this one waited.
    if ! runtime_marked; then
        rm -f "$marker"
        # Over what is there, which keeps Extras already installed into it and
        # finishes one an interrupted start left without pip or without the
        # package's own libraries. Cleared only if that does not work.
        if ! { "$base" -m venv --system-site-packages "$runtime" && runtime_works; }; then
            "$base" -m venv --clear --system-site-packages "$runtime"
            if ! runtime_works; then
                echo "ninaivu: could not prepare $runtime" >&2
                exit 70
            fi
        fi
        printf '%s\n' "$stamp" > "$marker.$$"
        mv -f "$marker.$$" "$marker"
    fi
    release_lock
    trap - EXIT INT TERM HUP
fi
python="$runtime/bin/python3"
export NINAIVU_PACKAGE=deb
export NINAIVU_HOME="$data/control"
export NINAIVU_STATE_DIR="$data"
export NINAIVU_AI_MODELS_DIR="$data/ai-models"
export NINAIVU_LOG_DIR="$data/logs"
export NINAIVU_PROGRAM_DIR="$app"
export NINAIVU_PYTHON="$python"
export NINAIVU_LAUNCHER=/usr/bin/ninaivu
case "${1:-}" in
    --where)
        printf 'NINAIVU_HOME=%s\nNINAIVU_STATE_DIR=%s\nNINAIVU_AI_MODELS_DIR=%s\nNINAIVU_LOG_DIR=%s\nNINAIVU_PYTHON=%s\n' \
            "$NINAIVU_HOME" "$data" "$NINAIVU_AI_MODELS_DIR" "$NINAIVU_LOG_DIR" "$python"
        exit 0 ;;
    --autostart) shift; exec "$python" -m ninaivu.desktop.autostart --start "$@" ;;
    --server) shift; exec "$python" -m ninaivu "$@" ;;
esac
exec "$python" -m ninaivu.desktop.launch "$@"

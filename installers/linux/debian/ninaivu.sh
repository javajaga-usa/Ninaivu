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
runtime="$data/runtime"
if [ ! -x "$runtime/bin/python3" ]; then
    "$app/python/bin/python3" -m venv --system-site-packages "$runtime"
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

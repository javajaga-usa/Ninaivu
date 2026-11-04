#!/bin/sh
# Sourced from the checkout root by its terminal and desktop launchers.
# Existing local libraries keep their location; new apps use the user's home.
if [ -z "${NINAIVU_STATE_DIR:-}" ]; then
    if [ -f "$PWD/.ninaivu/index.db" ] || [ -f "$PWD/.ninaivu/index-location.json" ]; then
        NINAIVU_STATE_DIR="$PWD/.ninaivu"
    elif [ -f "$PWD/data/index.db" ] || [ -f "$PWD/data/index-location.json" ]; then
        NINAIVU_STATE_DIR="$PWD/data"
    elif [ -n "${XDG_DATA_HOME:-}" ] && { [ -f "$XDG_DATA_HOME/ninaivu/index.db" ] || [ -f "$XDG_DATA_HOME/ninaivu/index-location.json" ]; }; then
        NINAIVU_STATE_DIR="$XDG_DATA_HOME/ninaivu"
    else
        NINAIVU_STATE_DIR="$HOME/.ninaivu"
    fi
fi
export NINAIVU_STATE_DIR
export NINAIVU_HOME="${NINAIVU_HOME:-$PWD}"
export NINAIVU_PYTHON="${NINAIVU_PYTHON:-$PWD/.venv/bin/python}"
export NINAIVU_LOG_DIR="${NINAIVU_LOG_DIR:-$PWD/logs}"
export NINAIVU_LAUNCHER="$PWD/Start - Ninaivu Control Panel.sh"

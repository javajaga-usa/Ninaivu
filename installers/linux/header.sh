#!/bin/sh
# Ninaivu __VERSION__ for Linux (__ARCH__) — your family's photographs, on a computer at home.
# By Jagadeesh Rajendran. MIT licence.
#
#   sh Ninaivu-__VERSION__-linux-__ARCH__.sh [--prefix DIR] [--photos DIR] [--no-service] [--quiet]
#
# Everything is inside this file: a private Python, so the machine needs none
# of its own, and every package Ninaivu uses. Nothing is downloaded. Without
# root it installs under ~/.local/share/ninaivu; as root, under /opt/ninaivu.
set -e
tmp=$(mktemp -d "${TMPDIR:-/tmp}/ninaivu-install.XXXXXX")
trap 'rm -rf "$tmp"' EXIT INT TERM
lines=$(awk '/^__PAYLOAD_BELOW__$/ { print NR + 1; exit 0; }' "$0")
tail -n +"$lines" "$0" | tar -xzf - -C "$tmp"
sh "$tmp/install.sh" "$tmp" "$@"
exit 0
__PAYLOAD_BELOW__

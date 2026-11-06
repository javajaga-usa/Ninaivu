#!/usr/bin/env bash
# Install Debian/Ubuntu packages on a CI runner without letting a slow mirror
# take the job with it.
#
#   tools/ci_apt_install.sh ffmpeg xvfb ...
#   tools/ci_apt_install.sh --run npx playwright install --with-deps webkit
#
# GitHub's Ubuntu mirror (azure.archive.ubuntu.com) sometimes crawls: on
# 6 November 2026 the browser job's ffmpeg took 29 minutes, a package at a
# time with pauses of three, and the job's 30-minute limit ended it before a
# single test ran. A download that is slow but alive never trips apt's own
# timeouts, so each attempt here has a limit of its own, and an attempt that
# runs out:
#   - stops what it left behind. Playwright runs apt-get through sudo in a
#     process of its own, which outlived the limit and held apt's lock, so the
#     next two attempts failed in two seconds each (the same day, later);
#   - puts the next mirror of the runner's list first, so the next attempt
#     asks another server rather than the one that crawled;
#   - keeps what it fetched: apt's cache holds every whole package, and the
#     next attempt only fetches the rest.
# The settings go into apt's own configuration, so an apt-get that another
# program starts (Playwright's --with-deps) has them too. With CI_APT_CACHE
# set, packages are kept in that folder, for the workflow to cache between
# runs: a run that finds them asks no mirror for them at all.
#
# --run runs a command that installs through apt itself under the same limit
# and retries.
set -uo pipefail

ATTEMPTS=${CI_APT_ATTEMPTS:-3}
LIMIT=${CI_APT_SECONDS:-480}
CACHE=${CI_APT_CACHE:-}

# CI_APT_GET stands in for apt-get in the tests (test_ci_runners.py), which
# must never reach the real one; a stand-in is run without sudo, and with it
# CI_APT_CONF and CI_APT_MIRRORS name files of the test's own.
APT_GET=${CI_APT_GET:-apt-get}
CONF=${CI_APT_CONF:-/etc/apt/apt.conf.d/99ci-apt-install}
MIRRORS=${CI_APT_MIRRORS:-/etc/apt/apt-mirrors.txt}
SUDO=()
if [ "$(id -u)" -ne 0 ] && [ -z "${CI_APT_GET:-}" ]; then
  SUDO=(sudo)
fi
export DEBIAN_FRONTEND=noninteractive

settings='Acquire::Retries "3";
Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
DPkg::Lock::Timeout "300";
Dpkg::Use-Pty "0";'
if [ -n "$CACHE" ]; then
  mkdir -p "$CACHE/partial" 2>/dev/null || "${SUDO[@]}" mkdir -p "$CACHE/partial"
  settings+=$'\n'"Dir::Cache::Archives \"$CACHE/\";"
  # A folder in the runner's home, where apt's download user cannot write.
  settings+=$'\n''APT::Sandbox::User "root";'
  echo "packages kept in $CACHE: $(find "$CACHE" -maxdepth 1 -name '*.deb' 2>/dev/null | wc -l) there already"
fi
if ! printf '%s\n' "$settings" | "${SUDO[@]}" tee "$CONF" >/dev/null; then
  echo "::warning::could not write $CONF; apt runs with its own settings"
fi

if [ "${1:-}" = "--run" ]; then
  shift
  command=("$@")
else
  command=("${SUDO[@]}" "$APT_GET" install -y -q --no-install-recommends "$@")
fi

stop_leftovers() {
  # Whatever the attempt started and the limit did not reach (an apt-get
  # under sudo, started by Playwright) would hold apt's lock.
  local name
  name=$(basename "$APT_GET")
  "${SUDO[@]}" pkill -TERM -x "$name" 2>/dev/null && sleep 3
  "${SUDO[@]}" pkill -KILL -x "$name" 2>/dev/null
  if [ -z "${CI_APT_GET:-}" ]; then
    # A dpkg that was cut short is finished first; a dpkg still running
    # is waited for (DPkg::Lock::Timeout).
    "${SUDO[@]}" dpkg --configure -a >/dev/null 2>&1 || true
  fi
}

next_mirror() {
  # The runner's list (apt's mirror+file method) is tried in order, and only
  # a mirror that fails is passed over; one that crawls is not. Each line
  # keeps its own settings (the priority); the addresses move up by one.
  [ -f "$MIRRORS" ] || return 0
  local turned
  turned=$(awk -F'\t' -v OFS='\t' '
    /^[[:space:]]*(#|$)/ { next }
    { n++; url[n] = $1; line[n] = $0 }
    END {
      if (n < 2) exit 1
      for (i = 1; i <= n; i++) { $0 = line[i]; $1 = url[i % n + 1]; print }
    }' "$MIRRORS") || return 0
  if printf '%s\n' "$turned" | "${SUDO[@]}" tee "$MIRRORS" >/dev/null; then
    echo "::notice::asking ${turned%%[[:space:]]*} first next time"
  fi
}

updated=0
for attempt in $(seq 1 "$ATTEMPTS"); do
  if [ "$updated" -eq 0 ]; then
    timeout --kill-after=20 "$LIMIT" "${SUDO[@]}" "$APT_GET" update -q
    status=$?
    if [ "$status" -eq 0 ]; then
      updated=1
    else
      echo "::warning::apt-get update did not finish in ${LIMIT}s (attempt $attempt of $ATTEMPTS; exit $status)"
      stop_leftovers
      next_mirror
      continue
    fi
  fi
  timeout --kill-after=20 "$LIMIT" "${command[@]}"
  status=$?
  if [ "$status" -eq 0 ]; then
    exit 0
  fi
  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
    echo "::warning::'${command[*]}' did not finish in ${LIMIT}s (attempt $attempt of $ATTEMPTS); trying again"
  else
    echo "::warning::'${command[*]}' failed with exit $status (attempt $attempt of $ATTEMPTS); trying again"
  fi
  stop_leftovers
  next_mirror
done
echo "::error::'${command[*]}' did not succeed in $ATTEMPTS attempts of up to ${LIMIT}s: the package mirror is too slow or down"
exit 1

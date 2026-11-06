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
# timeouts, so each attempt here has a limit of its own. What an attempt
# fetched stays in apt's cache, so the next one only fetches the rest, over a
# fresh connection (usually to a quicker mirror node).
#
# --run runs a command that installs through apt itself (Playwright's
# --with-deps) under the same limit and retries.
set -uo pipefail

ATTEMPTS=${CI_APT_ATTEMPTS:-3}
LIMIT=${CI_APT_SECONDS:-480}
APT_OPTS=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Dpkg::Use-Pty=0)

# CI_APT_GET stands in for apt-get in the tests (test_ci_runners.py), which
# must never reach the real one; a stand-in is run without sudo.
APT_GET=${CI_APT_GET:-apt-get}
SUDO=()
if [ "$(id -u)" -ne 0 ] && [ -z "${CI_APT_GET:-}" ]; then
  SUDO=(sudo)
fi
export DEBIAN_FRONTEND=noninteractive

if [ "${1:-}" = "--run" ]; then
  shift
  command=("$@")
else
  command=("${SUDO[@]}" "$APT_GET" install -y -q --no-install-recommends "${APT_OPTS[@]}" "$@")
fi

updated=0
for attempt in $(seq 1 "$ATTEMPTS"); do
  if [ "$updated" -eq 0 ]; then
    if timeout "$LIMIT" "${SUDO[@]}" "$APT_GET" update -q "${APT_OPTS[@]}"; then
      updated=1
    else
      echo "::warning::apt-get update did not finish in ${LIMIT}s (attempt $attempt of $ATTEMPTS)"
      continue
    fi
  fi
  if timeout "$LIMIT" "${command[@]}"; then
    exit 0
  fi
  echo "::warning::'${command[*]}' did not finish in ${LIMIT}s (attempt $attempt of $ATTEMPTS); trying again"
done
echo "::error::'${command[*]}' did not finish after $ATTEMPTS attempts of ${LIMIT}s: the package mirror is too slow or down"
exit 1

#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"
PROFILE_DIR="${HERMES_BROWSER_CDP_PROFILE:-${HERMES_HOME}/browser-cdp-profile}"
CACHE_DIR="${HERMES_BROWSER_CDP_CACHE:-${HERMES_HOME}/browser-cdp-cache}"
ADDRESS="${HERMES_BROWSER_CDP_ADDRESS:-127.0.0.1}"
PORT="${HERMES_BROWSER_CDP_PORT:-9222}"

find_chrome() {
  if [[ -n "${HERMES_BROWSER_CDP_EXECUTABLE:-}" ]]; then
    printf '%s\n' "${HERMES_BROWSER_CDP_EXECUTABLE}"
    return 0
  fi

  local cache_root="${HOME:-/home/hermes}/.cache/ms-playwright"
  if [[ -d "${cache_root}" ]]; then
    find "${cache_root}" \
      -type f \
      -path '*/chrome-linux*/chrome' \
      -perm -111 \
      | sort -V \
      | tail -n 1
  fi
}

CHROME="$(find_chrome)"
if [[ -z "${CHROME}" || ! -x "${CHROME}" ]]; then
  echo "No executable Playwright Chromium was found. Run: agent-browser install --with-deps" >&2
  exit 1
fi

mkdir -p "${PROFILE_DIR}" "${CACHE_DIR}"

exec "${CHROME}" \
  --headless=new \
  "--remote-debugging-address=${ADDRESS}" \
  "--remote-debugging-port=${PORT}" \
  "--user-data-dir=${PROFILE_DIR}" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --no-first-run \
  --no-default-browser-check \
  --disable-background-networking \
  --disable-sync \
  --disable-extensions \
  --disable-crash-reporter \
  about:blank

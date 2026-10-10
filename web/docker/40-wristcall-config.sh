#!/bin/sh
# Runs at container start (the nginx image's entrypoint runs everything in /docker-entrypoint.d/).
# Writes /tmp/wristcall/config.json, which nginx serves as /config.json: the page reads the Cloud address there, so
# one image fits any deployment. WRISTCALL_WEB_CLOUD_URL is empty (no account features), https://host[:port], or
# http://localhost|127.0.0.1[:port] for development. Anything else stops the container.
set -eu

url="${WRISTCALL_WEB_CLOUD_URL:-}"
newline='
'

fail() {
  echo "40-wristcall-config: WRISTCALL_WEB_CLOUD_URL must be empty, https://host[:port], or http://localhost[:port] (got an invalid value)" >&2
  exit 1
}

if [ -n "$url" ]; then
  # A newline would let a multi-line value pass the line-based match below.
  case "$url" in
    *"$newline"*) fail ;;
  esac
  # The patterns allow only letters, digits, dots, hyphens, colon and the scheme: no quote or backslash can get into the JSON.
  printf '%s\n' "$url" | grep -Eq '^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$|^http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?$' || fail
fi

mkdir -p /tmp/wristcall
printf '{"cloudUrl": "%s"}\n' "$url" > /tmp/wristcall/config.json

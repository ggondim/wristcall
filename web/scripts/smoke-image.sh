#!/usr/bin/env bash
# Smoke test of the built image, run by CI and locally: ./scripts/smoke-image.sh [image] [container-name] [host-port]
# Starts the image, checks the headers and routes it must serve, then checks that bad configuration stops it.
set -euo pipefail

IMAGE="${1:-wristcall-web}"
NAME="${2:-wristcall-web-smoke}"
PORT="${3:-8080}"
BASE="http://localhost:${PORT}"
WORK="$(mktemp -d)"

cleanup() {
  docker rm -f "$NAME" "${NAME}-refuse" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
  echo "smoke: FAIL: $*" >&2
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then docker logs "$NAME" >&2 || true; fi
  exit 1
}

# 1. Bad Cloud addresses stop the container, with a message that names the variable; good ones start it.
refuses() {
  local value="$1" state="" i
  docker rm -f "${NAME}-refuse" >/dev/null 2>&1 || true
  docker run -d -e "WRISTCALL_WEB_CLOUD_URL=${value}" --name "${NAME}-refuse" "$IMAGE" >/dev/null
  for i in $(seq 20); do
    state="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "${NAME}-refuse")"
    [ "${state%% *}" = exited ] && break
    sleep 1
  done
  [ "${state%% *}" = exited ] || fail "container still running with WRISTCALL_WEB_CLOUD_URL=${value@Q}"
  [ "${state##* }" != 0 ] || fail "container exited 0 with WRISTCALL_WEB_CLOUD_URL=${value@Q}"
  docker logs "${NAME}-refuse" 2>&1 | grep -q WRISTCALL_WEB_CLOUD_URL || fail "refusal for ${value@Q} does not name the variable"
  docker rm -f "${NAME}-refuse" >/dev/null
}
refuses 'http://evil.example'
refuses 'https://a.example/"x'
refuses 'https://a.example/path'
refuses 'https://a.example?x=1'
refuses 'https://a.example\x'
refuses 'ftp://a.example'
refuses $'https://a.example\nhttps://b.example'
echo "smoke: refusals ok"

serves_config() {
  local value="$1" expected="$2" cid
  cid="$(docker run -d -p "${PORT}:8080" -e "WRISTCALL_WEB_CLOUD_URL=${value}" --name "$NAME" "$IMAGE")"
  for _ in $(seq 30); do curl -sf "$BASE/" >/dev/null && break; sleep 1; done
  curl -sf "$BASE/config.json" | EXPECTED="$expected" python3 -c 'import json,os,sys; assert json.load(sys.stdin)=={"cloudUrl": os.environ["EXPECTED"]}' \
    || fail "config.json for ${value@Q}"
  docker rm -f "$cid" >/dev/null
}
serves_config '' ''
serves_config 'http://localhost:8090' 'http://localhost:8090'

# 2. The image served with a Cloud address.
docker run -d -p "${PORT}:8080" -e WRISTCALL_WEB_CLOUD_URL=https://cloud.example --name "$NAME" "$IMAGE" >/dev/null
for _ in $(seq 30); do curl -sf "$BASE/" >/dev/null && break; sleep 1; done
curl -sf "$BASE/" >/dev/null || fail "container did not answer on ${BASE}"
curl -sf "$BASE/config.json" | python3 -c 'import json,sys; assert json.load(sys.stdin)=={"cloudUrl": "https://cloud.example"}' || fail "config.json"

ASSET="$(curl -sf "$BASE/" | grep -oE '/assets/[A-Za-z0-9_.-]+\.js' | head -1)"
[ -n "$ASSET" ] || fail "index.html lists no /assets/ script"

# Headers (lowercased) and status of a GET.
head_of() { curl -s -o /dev/null -D - "$BASE$1" | tr -d '\r' | tr 'A-Z' 'a-z'; }
status_of() { curl -s -o /dev/null -w '%{http_code}' "$BASE$1"; }

# The security headers must be on every answer, including a 404 and the files with a Cache-Control of their own.
for path in / /index.html /servers /sw.js /config.json /manifest.webmanifest "$ASSET" /icons/icon-192.png /assets/missing.js /icons/missing.png; do
  headers="$(head_of "$path")"
  grep -q "^content-security-policy: default-src 'self'; script-src 'self';" <<<"$headers" || fail "$path: no content-security-policy"
  grep -q "frame-ancestors 'none'" <<<"$headers" || fail "$path: policy without frame-ancestors"
  if grep -q "unsafe-" <<<"$headers"; then fail "$path: policy has unsafe-*"; fi
  grep -qx 'x-content-type-options: nosniff' <<<"$headers" || fail "$path: no nosniff"
  grep -qx 'referrer-policy: no-referrer' <<<"$headers" || fail "$path: no referrer-policy"
  grep -q '^permissions-policy: camera=()' <<<"$headers" || fail "$path: no permissions-policy"
  if grep -q '^server: nginx/' <<<"$headers"; then fail "$path: server header shows the version"; fi
done
echo "smoke: headers ok"

for path in / /index.html /servers /sw.js /config.json /manifest.webmanifest; do
  grep -qx 'cache-control: no-cache' <<<"$(head_of "$path")" || fail "$path: cache-control is not no-cache"
done
grep -q '^cache-control: public, max-age=31536000, immutable' <<<"$(head_of "$ASSET")" || fail "$ASSET: not immutable"

# Routes: the app's paths return index.html; a missing file under /assets/ or /icons/ does not.
curl -sf "$BASE/index.html" >"$WORK/index.html"
curl -sf "$BASE/servers" >"$WORK/servers.html"
cmp -s "$WORK/index.html" "$WORK/servers.html" || fail "/servers is not index.html"
[ "$(status_of /assets/missing.js)" = 404 ] || fail "/assets/missing.js is not a 404"
[ "$(status_of /icons/missing.png)" = 404 ] || fail "/icons/missing.png is not a 404"
if grep -q '^cache-control:' <<<"$(head_of /assets/missing.js)"; then fail "a 404 carries a cache-control"; fi
grep -qi '^content-type: application/json' <<<"$(head_of /config.json)" || fail "/config.json content type"

# The build manifest is not published.
curl -sf "$BASE/.vite/manifest.json" >"$WORK/manifest.html"
cmp -s "$WORK/index.html" "$WORK/manifest.html" || fail "/.vite/manifest.json is served"
if docker exec "$NAME" test -e /usr/share/nginx/html/.vite; then fail "image has dist/.vite"; fi

# Not root.
[ "$(docker exec "$NAME" id -u)" != 0 ] || fail "container runs as root"
echo "smoke: ok"

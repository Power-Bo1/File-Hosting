#!/usr/bin/env bash
# Build the api + web images and PROVE them locally before pushing.
#
#   ./scripts/build-test.sh
#
# Checks, in order:
#   1. both images build
#   2. they run as uid 10001, not root
#   3. pip is gone from the runtime image
#   4. the api works end to end (register -> login -> upload -> list)
#      against a throwaway Postgres, with a READ-ONLY root filesystem
#   5. the web image serves its login page against that api
# Everything is torn down on exit. Nothing touches your cluster.
set -euo pipefail

# Every check goes through ok/fail. Bare `cmd && echo OK` is NOT safe:
# under `set -e`, a failing left-hand side of an && list does not exit the
# script, so a broken check silently prints nothing and the run still
# reports PASSED. fail() exits non-zero so the gate actually gates.
ok()   { printf '    %-32s OK\n' "$1"; }
fail() { printf '    %-32s FAIL\n' "$1"; exit 1; }

REGISTRY="${REGISTRY:-192.168.1.210:5000}"
TAG="${TAG:-2.13}"
NET=filehost-buildtest
PGPW=buildtest-only-pw
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cleanup() {
  docker rm -f bt-api bt-web bt-pg >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -f /tmp/bt-upload.txt
}
trap cleanup EXIT
cleanup

echo "==> 1/5 Building"
docker build -t "${REGISTRY}/file-api:${TAG}" "${HERE}/api"
docker build -t "${REGISTRY}/file-web:${TAG}" "${HERE}/web"

echo
echo "==> 2/5 Static image checks"
for name in file-api file-web; do
  img="${REGISTRY}/${name}:${TAG}"
  size=$(docker image inspect "$img" --format '{{.Size}}')
  echo "--- ${name}: $(( size / 1024 / 1024 )) MB"

  # NOTE: the runtime image is distroless - no shell, no id, no ls.
  # Every check goes through python, which is the one binary present.
  uid=$(docker run --rm --entrypoint python "$img" -c 'import os;print(os.getuid())')
  [ "$uid" = "65532" ] || { echo "FAIL: runs as uid ${uid}, expected 65532"; exit 1; }
  ok "uid 65532 (non-root)"

  if docker run --rm --entrypoint python "$img" -m pip --version >/dev/null 2>&1; then
    echo "FAIL: pip is present in the runtime image"; exit 1
  fi
  ok "no pip in runtime image"

  if docker run --rm --entrypoint sh "$img" -c 'echo hi' >/dev/null 2>&1; then
    echo "FAIL: a shell exists - this is not a distroless runtime"; exit 1
  fi
  ok "no shell (distroless)"

  files=$(docker run --rm --entrypoint python "$img" \
    -c "import os;print(len([f for f in os.listdir('/app') if f.endswith('.py')]))")
  [ "$files" -ge 1 ] || { echo "FAIL: no .py files under /app"; exit 1; }
  ok "${files} app file(s) present"
done

echo
echo "==> 3/5 Starting throwaway Postgres"
docker network create "$NET" >/dev/null
docker run -d --name bt-pg --network "$NET" \
  -e POSTGRES_DB=clientfiles -e POSTGRES_USER=appuser -e POSTGRES_PASSWORD="$PGPW" \
  postgres:17 >/dev/null
for i in $(seq 1 30); do
  docker exec bt-pg pg_isready -U appuser -d clientfiles >/dev/null 2>&1 && break
  sleep 2
done
echo "    postgres ready"

echo
echo "==> 4/5 API end-to-end (read-only rootfs, tmpfs /tmp and /data)"
docker run -d --name bt-api --network "$NET" \
  --read-only --tmpfs /tmp --tmpfs /data:mode=1777 \
  -e DB_HOST=bt-pg -e DB_NAME=clientfiles -e DB_USER=appuser -e DB_PASSWORD="$PGPW" \
  -e JWT_SECRET=buildtest-secret-long-enough-for-hmac-sha256 \
  -e MAX_UPLOAD_MB=5 \
  -p 18000:8000 "${REGISTRY}/file-api:${TAG}" >/dev/null

for i in $(seq 1 30); do
  curl -fsS http://localhost:18000/health >/dev/null 2>&1 && break
  sleep 2
  [ "$i" = "30" ] && { echo "FAIL: api never became healthy"; docker logs bt-api; exit 1; }
done
ok "/health"
curl -fsS http://localhost:18000/ready >/dev/null \
  && ok "/ready (reaches postgres)" || fail "/ready did not return 2xx"

curl -fsS -X POST http://localhost:18000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"buildtest","password":"longenough1","email":"bt@test.local"}' \
  >/dev/null && ok "register" || fail "register failed"

TOKEN=$(curl -fsS -X POST http://localhost:18000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"buildtest","password":"longenough1"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])') \
  || fail "login failed"
[ -n "$TOKEN" ] && ok "login (JWT issued)" || fail "login returned no token"

echo "hello from build-test" > /tmp/bt-upload.txt
curl -fsS -X POST http://localhost:18000/upload \
  -H "Authorization: Bearer ${TOKEN}" -F "file=@/tmp/bt-upload.txt" \
  >/dev/null && ok "upload (writes under RO root)" || fail "upload failed"

curl -fsS http://localhost:18000/files -H "Authorization: Bearer ${TOKEN}" \
  | grep -q bt-upload.txt && ok "file listed back" || fail "uploaded file not listed"

# NOTE: no -f here. curl -f exits 22 on 4xx and prints nothing to stdout,
# which made this check silently no-op in v2.6.1. We WANT the 401.
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18000/files)
case "$code" in
  401|403) ok "anonymous request rejected (${code})" ;;
  *)       fail "anonymous /files returned ${code}, expected 401/403" ;;
esac

# --- v2.8: logout revokes THIS session only -------------------------------
TOKEN2=$(curl -fsS -X POST http://localhost:18000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"buildtest","password":"longenough1"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])') \
  || fail "second login failed"
[ "$TOKEN2" != "$TOKEN" ] && ok "second login issues a distinct token" \
                          || fail "two logins returned the same token"

curl -fsS -X POST http://localhost:18000/auth/logout \
  -H "Authorization: Bearer ${TOKEN}" >/dev/null \
  && ok "logout accepted" || fail "logout failed"

code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18000/files \
  -H "Authorization: Bearer ${TOKEN}")
[ "$code" = "401" ] && ok "revoked token rejected (401)" \
                    || fail "revoked token returned ${code}, expected 401"

code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18000/files \
  -H "Authorization: Bearer ${TOKEN2}")
[ "$code" = "200" ] && ok "other session still valid (200)" \
                    || fail "second session returned ${code}, expected 200"

# --- v2.9: download + delete (uses TOKEN2, TOKEN is now revoked) ----------
body=$(curl -fsS http://localhost:18000/files/bt-upload.txt \
  -H "Authorization: Bearer ${TOKEN2}") || fail "download failed"
[ "$body" = "hello from build-test" ] && ok "download returns file content" \
                                      || fail "download content mismatch"

ctype=$(curl -sS -o /dev/null -D - -w '' http://localhost:18000/files/bt-upload.txt \
  -H "Authorization: Bearer ${TOKEN2}" | grep -i '^content-type:' | tr -d '\r')
printf '%s' "$ctype" | grep -q 'application/octet-stream' \
  && ok "download is octet-stream (never rendered)" \
  || fail "download content-type was: ${ctype}"

code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18000/files/nope.txt \
  -H "Authorization: Bearer ${TOKEN2}")
[ "$code" = "404" ] && ok "download of missing file 404" \
                    || fail "missing file returned ${code}"

code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
  http://localhost:18000/files/bt-upload.txt -H "Authorization: Bearer ${TOKEN2}")
[ "$code" = "200" ] && ok "delete accepted" || fail "delete returned ${code}"

code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18000/files/bt-upload.txt \
  -H "Authorization: Bearer ${TOKEN2}")
[ "$code" = "404" ] && ok "deleted file no longer downloadable (404)" \
                    || fail "deleted file returned ${code}"

curl -fsS http://localhost:18000/files -H "Authorization: Bearer ${TOKEN2}" \
  | grep -q bt-upload.txt \
  && fail "deleted file still listed" \
  || ok "deleted file gone from listing"

echo
echo "==> 5/5 Web image"
docker run -d --name bt-web --network "$NET" --read-only --tmpfs /tmp \
  -e API_URL=http://bt-api:8000 -e FLASK_SECRET_KEY=buildtest-key \
  -p 15000:5000 "${REGISTRY}/file-web:${TAG}" >/dev/null

for i in $(seq 1 20); do
  curl -fsS http://localhost:15000/health >/dev/null 2>&1 && break
  sleep 2
  [ "$i" = "20" ] && { echo "FAIL: web never became healthy"; docker logs bt-web; exit 1; }
done
ok "/health"
curl -fsS http://localhost:15000/login | grep -q "Forgot password" \
  && ok "login page renders" || fail "login page missing 'Forgot password'"

echo
echo "BUILD TEST PASSED - safe to push:"
echo "  docker push ${REGISTRY}/file-api:${TAG}"
echo "  docker push ${REGISTRY}/file-web:${TAG}"

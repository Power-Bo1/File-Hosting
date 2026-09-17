#!/usr/bin/env bash
# One-command restore drill: proves the NEWEST backup is restorable,
# without touching production data.
#
#   ./scripts/restore-drill.sh
#
# What it does:
#   1. Ensures the backup-shell helper pod exists and is Ready
#   2. Picks the newest /backups/clientfiles-*.dump automatically
#   3. Restores it into a scratch DB (clientfiles_drill)
#   4. Prints row counts: drill vs live (may differ if data changed
#      since the dump was taken - that's expected, not a failure)
#   5. Drops the scratch DB
set -euo pipefail

NS=fileapp
POD=backup-shell
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: kubectl cannot reach the cluster."
  [ -n "${SUDO_USER:-}" ] && echo "  Running under sudo? sudo resets \$HOME; run without it."
  exit 2
fi
if ! kubectl get pod -n "$NS" "$POD" >/dev/null 2>&1; then
  echo "==> Creating helper pod"
  kubectl apply -f "${ROOT}/k8s/backup-shell.yaml"
fi
echo "==> Waiting for helper pod to be Ready"
kubectl wait -n "$NS" --for=condition=Ready "pod/$POD" --timeout=120s

INNER=$(cat <<'EOS'
LATEST=$(ls -1t /backups/clientfiles-*.dump 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
  echo "DRILL FAIL: no dumps found in /backups"; exit 1
fi
echo "Newest backup: $LATEST ($(du -h "$LATEST" | cut -f1))"
dropdb  -h "$PGHOST" -U "$PGUSER" --if-exists clientfiles_drill
createdb -h "$PGHOST" -U "$PGUSER" clientfiles_drill
pg_restore -h "$PGHOST" -U "$PGUSER" -d clientfiles_drill "$LATEST"
DRILL_U=$(psql -h "$PGHOST" -U "$PGUSER" -d clientfiles_drill -tAc "SELECT count(*) FROM users")
DRILL_F=$(psql -h "$PGHOST" -U "$PGUSER" -d clientfiles_drill -tAc "SELECT count(*) FROM files")
LIVE_U=$(psql  -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE"    -tAc "SELECT count(*) FROM users")
LIVE_F=$(psql  -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE"    -tAc "SELECT count(*) FROM files")
echo "users: drill=$DRILL_U  live=$LIVE_U"
echo "files: drill=$DRILL_F  live=$LIVE_F"
dropdb -h "$PGHOST" -U "$PGUSER" clientfiles_drill
echo "DRILL PASS: newest backup restores cleanly"
EOS
)

kubectl exec -n "$NS" "$POD" -- bash -ceu "$INNER"

echo
echo "Done. Remove the helper pod when finished browsing:"
echo "  kubectl delete pod -n $NS $POD"

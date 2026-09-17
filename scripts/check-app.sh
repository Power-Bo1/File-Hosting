#!/usr/bin/env bash
# In-cluster application health checks - the layer below check-headers.sh.
#
#   ./scripts/check-app.sh
#
# Read-only except for a probe file written into each user upload
# directory and removed immediately.
#
# WHY THIS EXISTS: after any change to runAsUser/fsGroup, `touch /data/.w`
# is NOT a sufficient test. fsGroup fixes the volume ROOT; pre-existing
# subdirectories created under a different UID keep their old ownership.
# That is how /data was writable while /data/1 was root-owned 0755 and
# every upload 500'd. This script probes the per-user directories.
set -uo pipefail

NS="${NS:-fileapp}"
FAILED=0
ok()  { printf '    %-40s OK\n' "$1"; }
bad() { printf '    %-40s FAIL  %s\n' "$1" "${2:-}"; FAILED=1; }
note(){ printf '    %-40s %s\n' "$1" "${2:-}"; }

# Preflight. Without this, a broken kubectl makes every query return
# empty, and "nothing is wrong" reads identically to "I cannot see
# anything" - the script would report PASSED while blind.
if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: kubectl cannot reach the cluster."
  if [ -n "${SUDO_USER:-}" ]; then
    echo "  You are running under sudo. sudo resets \$HOME to /root, so kubectl"
    echo "  looks for /root/.kube/config and falls back to localhost:8080."
    echo "  Run this WITHOUT sudo - it needs no root privileges."
  else
    echo "  Check: kubectl cluster-info   and \$KUBECONFIG / ~/.kube/config"
  fi
  exit 2
fi
if ! kubectl get ns "$NS" >/dev/null 2>&1; then
  echo "ERROR: namespace '$NS' not found. Override with NS=<namespace>."
  exit 2
fi

echo "==> 1/7 Pods"
pods=$(kubectl get pods -n "$NS" --no-headers 2>/dev/null)
if [ -z "$pods" ]; then
  bad "pod list" "no pods returned for namespace ${NS}"
else
  notready=$(printf '%s\n' "$pods" | awk '$3!="Running" && $3!="Completed" {print $1" ("$3")"}')
  count=$(printf '%s\n' "$pods" | wc -l | tr -d ' ')
  if [ -z "$notready" ]; then ok "${count} pods Running/Completed"
  else bad "pods not ready" "$notready"; fi
fi

for d in api web postgres mailpit; do
  if kubectl get deploy "$d" -n "$NS" >/dev/null 2>&1; then
    want=$(kubectl get deploy "$d" -n "$NS" -o jsonpath='{.spec.replicas}')
    got=$(kubectl get deploy "$d" -n "$NS" -o jsonpath='{.status.readyReplicas}')
    [ "${got:-0}" = "$want" ] && ok "deploy/$d ${got}/${want} ready" \
                              || bad "deploy/$d" "${got:-0}/${want} ready"
  fi
done

echo
echo "==> 2/7 Service endpoints"
for s in api web postgres mailpit-smtp; do
  if kubectl get svc "$s" -n "$NS" >/dev/null 2>&1; then
    eps=$(kubectl get endpoints "$s" -n "$NS" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null)
    [ -n "$eps" ] && ok "svc/$s -> ${eps}" || bad "svc/$s" "no endpoints"
  else
    note "svc/$s" "not present (skipped)"
  fi
done

echo
echo "==> 3/7 Service DNS + API reachability (from a web pod)"
PY_REACH='
import json,socket,time,urllib.request
host="api.'"$NS"'.svc.cluster.local"
t=time.time(); ip=socket.gethostbyname(host); dns=time.time()-t
t=time.time(); body=urllib.request.urlopen(f"http://{host}:8000/ready",timeout=10).read(); rt=time.time()-t
print(json.dumps({"ip":ip,"dns":round(dns,3),"ready":body.decode(),"rt":round(rt,3)}))
'
if out=$(kubectl exec -n "$NS" deploy/web -- python -c "$PY_REACH" 2>&1); then
  dns=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["dns"])')
  rt=$(printf '%s'  "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin)["rt"])')
  ok "api resolves + /ready 200"
  awk -v d="$dns" 'BEGIN{exit !(d<1.0)}' && ok "DNS ${dns}s (<1s)" \
                                         || bad "DNS slow" "${dns}s - see the ndots:2 dnsConfig"
  awk -v r="$rt"  'BEGIN{exit !(r<2.0)}' && ok "/ready ${rt}s (<2s)" \
                                         || bad "/ready slow" "${rt}s"
else
  bad "api reachable from web" "$out"
fi

echo
echo "==> 4/7 Upload volume (/data and EVERY user directory)"
PY_DATA='
import os,sys
uid=os.getuid(); problems=[]; owned=[]
if not os.access("/data", os.W_OK): problems.append("/data not writable")
dirs=[d for d in sorted(os.listdir("/data")) if os.path.isdir(os.path.join("/data",d))]
for d in dirs:
    p=os.path.join("/data",d); t=os.path.join(p,".probe-check-app")
    try:
        open(t,"w").close(); os.remove(t)
    except OSError as e:
        problems.append(f"{p}: {e.strerror}")
for root,ds,fs in os.walk("/data"):
    for n in ds+fs:
        fp=os.path.join(root,n)
        try:
            if os.lstat(fp).st_uid != uid: owned.append(fp)
        except OSError: pass
print("UID", uid)
print("USERDIRS", len(dirs), " ".join(dirs) if dirs else "(none)")
print("PROBLEMS", len(problems))
for p in problems: print("  !", p)
print("FOREIGN", len(owned))
for f in owned[:8]: print("  ~", f)
'
if out=$(kubectl exec -n "$NS" deploy/api -- python -c "$PY_DATA" 2>&1); then
  printf '%s\n' "$out" | sed 's/^/      /'
  uid=$(printf '%s\n' "$out" | awk '/^UID/{print $2}')
  probs=$(printf '%s\n' "$out" | awk '/^PROBLEMS/{print $2}')
  foreign=$(printf '%s\n' "$out" | awk '/^FOREIGN/{print $2}')
  note "api uid" "$uid"
  [ "$probs" = "0" ]   && ok "every user directory writable" \
                       || bad "user directory not writable" "see ! lines - run the chown job in UPGRADE.md"
  [ "$foreign" = "0" ] && ok "all of /data owned by uid ${uid}" \
                       || bad "${foreign} path(s) owned by another uid" "chown -R ${uid}:${uid} /data"
else
  bad "could not probe /data" "$out"
fi

echo
echo "==> 5/7 Database schema"
q() { kubectl exec -n "$NS" deploy/postgres -- psql -U appuser -d clientfiles -tAc "$1" 2>/dev/null | tr -d '\r'; }
if [ "$(q 'SELECT 1')" != "1" ]; then
  bad "postgres query" "cannot run psql - schema checks below are unreliable"
fi
for t in users files password_resets revoked_tokens; do
  [ "$(q "SELECT to_regclass('public.${t}') IS NOT NULL")" = "t" ] \
    && ok "table ${t}" || bad "table ${t}" "missing"
done
[ "$(q "SELECT count(*) FROM pg_indexes WHERE indexname='files_user_filename_uq'")" = "1" ] \
  && ok "unique index files_user_filename_uq" || bad "unique index" "missing - re-uploads will duplicate"
[ "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='users' AND column_name='token_version'")" = "1" ] \
  && ok "users.token_version (session revocation)" || bad "users.token_version" "missing"
note "users / files rows" "$(q 'SELECT count(*) FROM users') / $(q 'SELECT count(*) FROM files')"
note "live revoked tokens" "$(q 'SELECT count(*) FROM revoked_tokens') (self-expiring)"
stale=$(q "SELECT count(*) FROM revoked_tokens WHERE expires_at < now()")
if   [ -z "$stale" ];    then bad  "denylist stale-row check" "query returned nothing"
elif [ "$stale" = "0" ]; then ok   "denylist has no stale rows"
else                          note "stale denylist rows" "${stale} (pruned on next logout)"; fi
orphans=$(q "SELECT count(*) FROM files WHERE user_id IS NULL")
[ "$orphans" = "0" ] && ok "no orphaned file rows" || note "orphaned file rows" "$orphans (pre-v2 uploads)"

echo
echo "==> 6/7 Database <-> disk consistency"
# Catches the class of problem that produced
#   "WARNING: <file> is recorded for user N but missing on disk"
# and the invisible pre-v2 blobs stranded at the volume root.
PY_DISK='
import os
out=[]
root_files=[]
for d in sorted(os.listdir("/data")):
    p=os.path.join("/data",d)
    if os.path.isdir(p):
        if d.isdigit():
            for n in sorted(os.listdir(p)):
                if os.path.isfile(os.path.join(p,n)): out.append(f"{d}/{n}")
    elif os.path.isfile(p):
        root_files.append(d)
print("DISK_BEGIN")
for x in out: print(x)
print("DISK_END")
print("ROOT_BEGIN")
for x in root_files: print(x)
print("ROOT_END")
'
if raw=$(kubectl exec -n "$NS" deploy/api -- python -c "$PY_DISK" 2>/dev/null); then
  # LC_ALL=C: sort must use byte order, or it disagrees with comm on
  # mixed-case names (hi.txt vs Ho.txt) and comm silently mis-pairs them.
  disk=$(printf '%s\n' "$raw" | sed -n '/^DISK_BEGIN$/,/^DISK_END$/p' | sed '1d;$d' | LC_ALL=C sort)
  stray=$(printf '%s\n' "$raw" | sed -n '/^ROOT_BEGIN$/,/^ROOT_END$/p' | sed '1d;$d')
  db=$(q "SELECT user_id::text || '/' || filename FROM files WHERE user_id IS NOT NULL" | LC_ALL=C sort)

  missing=$(LC_ALL=C comm -23 <(printf '%s\n' "$db" | grep -v '^$') <(printf '%s\n' "$disk" | grep -v '^$'))
  orphan=$(LC_ALL=C comm -13 <(printf '%s\n' "$db" | grep -v '^$') <(printf '%s\n' "$disk" | grep -v '^$'))

  note "rows / blobs" "$(printf '%s\n' "$db" | grep -vc '^$') / $(printf '%s\n' "$disk" | grep -vc '^$')"
  if [ -z "$missing" ]; then ok "every DB row has a blob"
  else bad "rows with no blob" "$(printf '%s' "$missing" | tr '\n' ' ')"; fi
  if [ -z "$orphan" ]; then ok "every blob has a DB row"
  else bad "blobs with no row" "$(printf '%s' "$orphan" | tr '\n' ' ')"; fi
  if [ -z "$stray" ]; then ok "no files stranded at /data root"
  else bad "files at /data root (pre-v2)" "$(printf '%s' "$stray" | tr '\n' ' ')"; fi
else
  bad "consistency check" "could not list /data"
fi

echo
echo "==> 7/7 Shared storage (RWX)"
mode=$(kubectl get pvc api-files-rwx -n "$NS" -o jsonpath='{.spec.accessModes[0]}' 2>/dev/null)
if [ "$mode" = "ReadWriteMany" ]; then ok "api-files-rwx is ReadWriteMany"
elif [ -n "$mode" ];              then bad "api-files-rwx access mode" "$mode"
else                                   note "api-files-rwx" "not present (still on RWO?)"; fi

reps=$(kubectl get pods -n "$NS" -l app=api --no-headers 2>/dev/null | wc -l | tr -d ' ')
nodes=$(kubectl get pods -n "$NS" -l app=api -o jsonpath='{.items[*].spec.nodeName}' 2>/dev/null \
        | tr ' ' '\n' | sort -u | wc -l | tr -d ' ')
note "api replicas / distinct nodes" "${reps} / ${nodes}"
[ "${reps:-0}" -ge 2 ] && ok "more than one api replica" \
                       || note "api replicas" "${reps} - RWX allows >1"

# The real proof: pod A writes, pod B must see it on the SAME volume.
pods=$(kubectl get pods -n "$NS" -l app=api -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
set -- $pods
if [ "$#" -ge 2 ]; then
  probe=".rwx-probe-$$"
  if kubectl exec -n "$NS" "$1" -- python -c "open('/data/${probe}','w').write('x')" 2>/dev/null; then
    if kubectl exec -n "$NS" "$2" -- python -c "
import os,sys; sys.exit(0 if os.path.isfile('/data/${probe}') else 1)" 2>/dev/null; then
      ok "pod B sees a file written by pod A"
    else
      bad "shared volume" "pod B cannot see pod A's write - NOT actually shared"
    fi
    kubectl exec -n "$NS" "$1" -- python -c "import os; os.remove('/data/${probe}')" 2>/dev/null || true
  else
    bad "shared volume write" "pod A could not write to /data"
  fi
else
  note "cross-pod write test" "skipped (needs 2 api replicas)"
fi

echo
echo "==> Backups"
# MAX_BACKUP_AGE_DAYS is the point of this section. Checking only that a
# CronJob EXISTS, or that it succeeded once, is how a backup rots
# unnoticed: nightly jobs never fire if the cluster is powered off at
# the scheduled hour, and the object still looks healthy.
MAX_BACKUP_AGE_DAYS="${MAX_BACKUP_AGE_DAYS:-2}"
age_days() {   # $1 = RFC3339 timestamp -> whole days ago, or empty
  local t
  t=$(date -d "$1" +%s 2>/dev/null) || return 1
  echo $(( ( $(date +%s) - t ) / 86400 ))
}
for cj in postgres-backup files-backup; do
  if ! kubectl get cronjob "$cj" -n "$NS" >/dev/null 2>&1; then
    note "cronjob/$cj" "not present (skipped)"; continue
  fi
  sched=$(kubectl get cronjob "$cj" -n "$NS" -o jsonpath='{.spec.schedule}')
  last_sched=$(kubectl get cronjob "$cj" -n "$NS" -o jsonpath='{.status.lastScheduleTime}')
  last_ok=$(kubectl get cronjob "$cj" -n "$NS" -o jsonpath='{.status.lastSuccessfulTime}')
  if [ -z "$last_sched" ] && [ -z "$last_ok" ]; then
    note "cronjob/$cj" "created, not scheduled yet (${sched}) - trigger one to verify"
  elif [ -z "$last_ok" ]; then
    bad "cronjob/$cj" "has been scheduled but never succeeded"
  else
    d=$(age_days "$last_ok" || echo "")
    if [ -z "$d" ]; then
      note "cronjob/$cj" "last success ${last_ok} (could not parse age)"
    elif [ "$d" -le "$MAX_BACKUP_AGE_DAYS" ]; then
      ok "cronjob/$cj succeeded ${d}d ago"
    else
      bad "cronjob/$cj BACKUP IS STALE" "last success ${d} days ago (${last_ok})"
    fi
  fi
done

echo
if [ "$FAILED" -eq 0 ]; then echo "ALL APP CHECKS PASSED"; else echo "SOME CHECKS FAILED"; fi
exit "$FAILED"
